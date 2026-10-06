from __future__ import annotations
import argparse
import asyncio
import logging
import time

import yaml

from .chains.evm import EvmAdapter
from .chains.solana import SolanaAdapter
from .models import Position, Signal
from .positions import PositionManager
from .risk import RiskManager
from .store import Store
from .xchain.watcher import XchainWatcher

log = logging.getLogger("main")



def make_adapter(name: str, c: dict, paper: bool):
    kind = c.get("type", "solana" if name == "solana" else "evm")
    if kind == "solana":
        return SolanaAdapter(c, paper)
    if kind == "evm":
        return EvmAdapter(name, c, paper)
    raise ValueError(f"unknown chain type {kind}")


class Bot:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.paper = bool(cfg.get("paper", True))
        self.store = Store(cfg.get("db_path", "copybot.db"))
        self.adapters = {name: make_adapter(name, c, self.paper)
                         for name, c in cfg["chains"].items() if c.get("enabled")}
        self.chain_cfg = {n: cfg["chains"][n] for n in self.adapters}
        self.pm = PositionManager(cfg["exits"], self.adapters, self.store)
        self.risk = RiskManager(cfg["risk"], self.store, self.paper)
        self.in_flight: set[tuple[str, str]] = set()
        self.queue: asyncio.Queue = asyncio.Queue()
        for name, a in self.adapters.items():
            a.set_price_callback(lambda token, chain=name: self.pm.on_tick(chain, token))
            a.set_tick_sink(self.store.record_ticks)
            a.set_factor_sink(self.store.record_factor)
            a.set_event_sink(self.store.record_event)
        xc = cfg.get("xchain") or {}
        self.xchain = None
        if xc.get("enabled") and "solana" in self.adapters:   # watch-only: never trades
            self.xchain = XchainWatcher(xc, self.store)
            self.adapters["solana"].set_xchain_sink(self.xchain.on_solana_tx)

    def _skip(self, s: Signal, why: str):
        self.store.log_signal(s, "skip", why)
        a = self.adapters.get(s.chain)
        if a is not None:    # keep recording its price path: what would have happened?
            asyncio.ensure_future(a.observe(s.token))

    async def handle_buy(self, s: Signal):
        ccfg = self.chain_cfg[s.chain]
        age = time.time() - s.block_time if s.block_time else 0
        if age > float(ccfg.get("max_signal_age_s", 5)):
            self._skip(s, f"stale {age:.1f}s")
            return
        # only copy a leader's FIRST buy of a token: skip add-on buys (he already held it).
        # Dust left over from an earlier exit (< 5% of this buy) doesn't count as holding.
        if (ccfg.get("only_first_leader_buy") and s.leader_pre_amount
                and s.leader_pre_amount > 0.05 * max(1, s.token_amount)):
            self._skip(s, "leader already held this token (add-on buy)")
            return
        ok, why = self.risk.can_open(s.chain, s.token, self.pm.open(), self.in_flight, ccfg)
        if not ok:
            self._skip(s, why)
            return
        timing = {"leader_slot": s.slot, "leader_block_time": s.block_time,
                  "leader_pre_amount": s.leader_pre_amount, "leader_buy_amount": s.token_amount,
                  "notified_at": s.notified_at, "fetched_at": s.fetched_at, "decided_at": time.time()}
        key = (s.chain, s.token)
        self.in_flight.add(key)
        try:
            adapter = self.adapters[s.chain]
            size = float(ccfg["buy_amount"])
            ok, why = await adapter.pre_trade_check(s.token, size)
            timing["checked_at"] = time.time()
            if not ok:
                self.store.log_signal(s, "skip", why)
                return
            fill = await adapter.buy(s.token, size)
            timing.update(filled_at=time.time(), fill_slot=fill.slot, fill_model=fill.model)
            if not fill.ok:
                self.store.log_signal(s, "buy_failed", fill.error or "")
                log.error("BUY FAILED %s: %s", s.token, fill.error)
                return
            fill_mc = adapter.fill_market_cap(s.token, fill)
            if fill_mc:
                timing["fill_mc_usd"] = round(fill_mc)
            p = Position(None, s.chain, s.token, s.leader, fill.token_amount, fill.quote_amount,
                         time.time(), fill.signature, self.paper, timing=timing)
            p.id = self.store.open_position(p)
            self.pm.add(p)
            self.store.log_signal(s, "bought", why)
            t0 = s.notified_at or s.seen_at
            slots = (f"{fill.slot - s.slot} slots behind leader" if fill.slot and s.slot else "slots n/a")
            log.info("BOUGHT %s cost=%.4f | FILLED AT MC %s%s | fetch %.2fs, checks %.2fs, fill %.2fs, "
                     "total %.2fs, %s | check: %s [%s]",
                     s.token, fill.quote_amount,
                     f"${fill_mc:,.0f}" if fill_mc else "n/a",
                     "" if not self.paper else " (paper: bot scale)",
                     (s.fetched_at or t0) - t0, timing["checked_at"] - timing["decided_at"],
                     timing["filled_at"] - timing["checked_at"], timing["filled_at"] - t0, slots,
                     why, fill.model or "-")
        finally:
            self.in_flight.discard(key)

    async def consume(self):
        while True:
            s: Signal = await self.queue.get()
            log.info("signal %s %s %s %.3f by %s via %s", s.chain, s.side, s.token, s.quote_amount,
                     s.leader[:8], getattr(s, "venues", "") or "-")
            if s.side == "buy":
                asyncio.create_task(self.handle_buy(s))
            else:
                self.store.log_signal(s, "leader_sell")
                asyncio.create_task(self.pm.on_leader_sell(s.chain, s.leader, s.token))

    async def run(self):
        for a in self.adapters.values():
            await a.start()
        for p in self.store.load_open(self.paper):
            log.warning("resuming open position %s", p.token)
            self.pm.add(p)
        tasks = [asyncio.create_task(a.listen(self.queue)) for a in self.adapters.values()]
        tasks += [asyncio.create_task(self.consume()), asyncio.create_task(self.pm.run())]
        if self.xchain:
            tasks.append(asyncio.create_task(self.xchain.run()))
        log.info("running: chains=%s paper=%s", list(self.adapters), self.paper)
        try:
            await asyncio.gather(*tasks)
        finally:
            for a in self.adapters.values():
                await a.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-c", "--config", default="config.yaml")
    args = ap.parse_args()
    cfg = yaml.safe_load(open(args.config))
    logging.basicConfig(level=cfg.get("log_level", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=[logging.StreamHandler(), logging.FileHandler(cfg.get("log_file", "copybot.log"))])
    asyncio.run(Bot(cfg).run())


if __name__ == "__main__":
    main()
