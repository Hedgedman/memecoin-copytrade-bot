from __future__ import annotations
import argparse
import asyncio
import logging
import os
import time

import yaml

from .chains.evm import EvmAdapter
from .chains.solana import SolanaAdapter
from .models import Position, Signal
from .positions import PositionManager
from .profiles import load_profiles
from .risk import RiskManager
from .store import Store
from .xchain.paper import XchainPaperTrader
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
        self.profiles = load_profiles(cfg, self.paper)          # per-leader exits + sizing (paper only)
        # leader status (chains.<chain>.leader_status: {wallet: active|watch}): a WATCH leader is streamed and his
        # signals, entry gaps (ticks) and post-signal price paths are recorded, but he opens no positions anywhere
        self.watch: set[str] = set()
        for name, c in cfg["chains"].items():
            for leader, status in (c.get("leader_status") or {}).items():
                if status not in ("active", "watch"):
                    raise ValueError(f"chains.{name}.leader_status.{leader}: unknown status {status!r}")
                if status == "watch":
                    self.watch.add(leader)
        # leader_allowlist (chains.<chain>.leader_allowlist, optional list, Oct 8 2026 / H8-LIVE): ONLY these leaders
        # open positions; every other streamed leader becomes watch (signals + price paths recorded, no positions)
        for name, c in cfg["chains"].items():
            allow = c.get("leader_allowlist")
            if not allow:
                continue
            unknown = [w for w in allow if w not in (c.get("leaders") or [])]
            if unknown:
                raise ValueError(f"chains.{name}.leader_allowlist: {[w[:8] for w in unknown]} not in chains.{name}.leaders "
                                 f"(a leader must be streamed to be copied)")
            others = [w for w in (c.get("leaders") or []) if w not in allow]
            self.watch.update(others)
            log.info("leader allowlist (%s): %d of %d leaders open positions (%s); %d watch-only", name, len(allow),
                     len(c.get("leaders") or []), ", ".join(w[:8] for w in allow), len(others))
            for w in allow:
                if w in self.watch:
                    raise ValueError(f"chains.{name}.leader_allowlist: {w[:8]} is also leader_status watch")
        if self.watch:
            log.info("watch-only leaders (signals recorded, no positions): %s", ", ".join(w[:8] for w in sorted(self.watch)))
        r = cfg["risk"]
        log.info("risk: max_open_positions %s, reentry_cooldown_s %s, kill switch file %s; daily_loss_limit %s "
                 "(realized PnL of closed normal positions since 00:00 UTC; blocks new buys only, open positions "
                 "keep their exits); buy_amount %s", r.get("max_open_positions", 3), r.get("reentry_cooldown_s", 1800),
                 r.get("kill_switch_file", "STOP"),
                 ", ".join(f"{n} {c.get('daily_loss_limit', 0)}" for n, c in cfg["chains"].items() if c.get("enabled")),
                 ", ".join(f"{n} {c.get('buy_amount')}" for n, c in cfg["chains"].items() if c.get("enabled")))
        self.in_flight: set[tuple[str, str, str]] = set()        # (chain, token, profile name or "")
        if "solana" in self.adapters:                             # profile leaders: min_usd instead of min_leader_buy_sol
            self.adapters["solana"].min_buy_exempt = set(self.profiles)
        self.queue: asyncio.Queue = asyncio.Queue()
        for name, a in self.adapters.items():
            a.set_price_callback(lambda token, chain=name: self.pm.on_tick(chain, token))
            a.set_tick_sink(self.store.record_ticks)
            a.set_factor_sink(self.store.record_factor)
            a.set_event_sink(self.store.record_event)
        xc = cfg.get("xchain") or {}
        self.xchain = None
        if xc.get("enabled") and "solana" in self.adapters:   # watch-only: never trades
            sol = cfg["chains"]["solana"]
            self.xchain = XchainWatcher(xc, self.store, solana_leaders=(sol.get("leaders") or [])
                                        if xc.get("all_solana_leaders", True) else None, ws_url=sol.get("rpc_ws", ""))
            self.adapters["solana"].set_xchain_sink(self.xchain.on_solana_tx)
            pt = xc.get("paper_trading") or {}
            if pt.get("enabled"):                              # simulated fills only, own limits
                if self.paper:
                    self.xchain.trader = XchainPaperTrader(pt, self.store, self.paper, self.xchain.sol_leaders,
                                                           cfg["risk"].get("kill_switch_file", "STOP"),
                                                           profiles=self.profiles, global_exits=cfg.get("exits") or {},
                                                           watch=self.watch)
                else:
                    log.error("xchain.paper_trading ignored: it is paper-only and this bot runs with paper: false")

    def _skip(self, s: Signal, why: str):
        log.info("skip %s (%s): %s", s.token, s.leader[:8], why)
        self.store.log_signal(s, "skip", why)
        a = self.adapters.get(s.chain)
        if a is not None:    # keep recording its price path: what would have happened?
            asyncio.ensure_future(a.observe(s.token))

    def _profile_position(self, chain: str, token: str, leader: str) -> Position | None:
        """Our open position of a profile leader in this token (profile positions are keyed by leader + token)."""
        return next((p for p in self.pm.open() if p.chain == chain and p.token == token and p.leader == leader
                     and p.timing.get("profile")), None)

    def _profile_can_open(self, prof, sol_usd: float, key: tuple) -> str | None:
        """Own limits of a leader profile (new positions only; adds are capped by max_usd_per_token)."""
        if os.path.exists(self.cfg["risk"].get("kill_switch_file", "STOP")):
            return "kill switch file present"
        n = self.store.profile_open_count(prof.name) + sum(k[2] == prof.name and k != key for k in self.in_flight)
        if n >= prof.max_open_tokens:
            return f"profile {prof.name}: max open tokens ({prof.max_open_tokens})"
        lost = self.store.profile_pnl_today_usd(prof.name, sol_usd)
        if lost <= -prof.daily_loss_usd:
            return f"profile {prof.name}: daily loss limit ${prof.daily_loss_usd:,.0f} hit (${lost:,.0f})"
        return None

    async def handle_buy(self, s: Signal):
        ccfg = self.chain_cfg[s.chain]
        age = time.time() - s.block_time if s.block_time else 0
        if age > float(ccfg.get("max_signal_age_s", 5)):
            self._skip(s, f"stale {age:.1f}s")
            return
        if s.leader in self.watch:
            self._skip(s, "watch leader")                  # recorded + price path observed, never a position
            return
        prof = self.profiles.get(s.leader) if s.chain == "solana" else None
        held = self._profile_position(s.chain, s.token, s.leader) if prof is not None else None
        # only copy a leader's FIRST buy of a token: skip add-on buys (he already held it).
        # Dust left over from an earlier exit (< 5% of this buy) doesn't count as holding.
        # Profile leaders: his adds ARE the signal (copied into our position, 3.3), so no first-buy filter.
        if (prof is None and ccfg.get("only_first_leader_buy") and s.leader_pre_amount
                and s.leader_pre_amount > 0.05 * max(1, s.token_amount)):
            self._skip(s, "leader already held this token (add-on buy)")
            return
        if prof is None:
            # normal leaders: the global limits, counted over NORMAL positions only (profile positions are
            # separate: they never block a token, never count toward max_open / daily loss)
            normal_open = [p for p in self.pm.open() if not p.timing.get("profile")]
            normal_flight = {(c, t) for c, t, who in self.in_flight if not who}
            ok, why = self.risk.can_open(s.chain, s.token, normal_open, normal_flight, ccfg)
            if not ok:
                self._skip(s, why)
                return
        timing = {"leader_slot": s.slot, "leader_block_time": s.block_time,
                  "leader_pre_amount": s.leader_pre_amount, "leader_buy_amount": s.token_amount,
                  "notified_at": s.notified_at, "fetched_at": s.fetched_at, "decided_at": time.time()}
        key = (s.chain, s.token, prof.name if prof is not None else "")
        if key in self.in_flight:
            self._skip(s, "already buying this token")
            return
        self.in_flight.add(key)
        try:
            adapter = self.adapters[s.chain]
            size = float(ccfg["buy_amount"])
            if prof is not None:
                # proportional: his USD size x factor (s.quote_amount is SOL, also for his USDC buys)
                try:
                    sol_usd = await adapter.sol_usd()
                except Exception as e:
                    self._skip(s, f"profile {prof.name}: SOL price unavailable ({type(e).__name__})")
                    return
                already = float(held.timing.get("bought_usd") or held.timing.get("our_usd") or 0) if held else 0.0
                usd, why = prof.size_usd(s.quote_amount * sol_usd, already)   # trimmed to the room left
                if why:
                    self._skip(s, why)
                    return
                if held is None:
                    why = self._profile_can_open(prof, sol_usd, key)
                    if why:
                        self._skip(s, why)
                        return
                size = usd / sol_usd
                timing.update(prof.stamp(), his_usd=round(s.quote_amount * sol_usd, 2), our_usd=round(usd, 2),
                              bought_usd=round(usd, 2))
            # the leader's fill price (lamports per raw unit): sanity reference for the adapter's pool model
            ref = s.quote_amount * 1e9 / s.token_amount if s.chain == "solana" and s.token_amount else None
            ok, why = await adapter.pre_trade_check(s.token, size, ref_price=ref)
            timing["checked_at"] = time.time()
            if not ok:
                log.info("skip %s (%s): %s", s.token, s.leader[:8], why)
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
            if held is not None:                                     # profile add: merge into our position
                self._merge_add(held, fill, usd, timing)
                self.store.log_signal(s, "bought", f"add #{held.timing['adds']} ({why})")
                log.info("ADD %s (%s): +%.4f SOL for his add, position now %.4f SOL / %d raw, $%.0f of $%.0f cap, "
                         "avg entry %.3g SOL/raw [%s]", s.token, prof.name, fill.quote_amount, held.cost,
                         held.token_amount, held.timing["bought_usd"], prof.max_usd_per_token,
                         held.cost / int(held.timing.get("initial_amount") or held.token_amount), fill.model or "-")
                return
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

    def _merge_add(self, p: Position, fill, usd: float, timing: dict):
        """Weighted merge of an add-on fill into an open profile position: amount, cost, bought_usd, the stop
        reference and the trailing peak grow by the add (so the add itself never trips a stop); the average
        entry (cost / all tokens) rises with it, and so do the ladder levels. Adds are listed in timing["add_ts"]."""
        p.token_amount += fill.token_amount
        p.cost += fill.quote_amount
        if "initial_amount" in p.timing:                             # partial sells happened: keep `held` consistent
            p.timing["initial_amount"] = int(p.timing["initial_amount"]) + fill.token_amount
        p.timing["bought_usd"] = round(float(p.timing.get("bought_usd") or p.timing.get("our_usd") or 0) + usd, 2)
        p.timing["adds"] = int(p.timing.get("adds", 0)) + 1
        p.timing.setdefault("add_ts", []).append(round(timing.get("filled_at") or time.time(), 3))
        p.timing.setdefault("add_fills", []).append({"sol": fill.quote_amount, "raw": fill.token_amount,
                                                     "usd": round(usd, 2), "model": fill.model})
        if p.ref_value is not None:
            p.ref_value += fill.quote_amount
            self.store.set_ref(p)
        if p.peak_value is not None:
            p.peak_value += fill.quote_amount
        self.store.update_position(p)

    async def consume(self):
        while True:
            s: Signal = await self.queue.get()
            log.info("signal %s %s %s %.3f by %s via %s", s.chain, s.side, s.token, s.quote_amount,
                     s.leader[:8], getattr(s, "venues", "") or "-")
            if s.side == "buy":
                asyncio.create_task(self.handle_buy(s))
            else:
                self.store.log_signal(s, "leader_sell")
                # share of his holding he sold (pre-balance from his sell tx); unknown -> treat as all
                frac = min(1.0, s.token_amount / s.leader_pre_amount) if s.leader_pre_amount > 0 else 1.0
                asyncio.create_task(self.pm.on_leader_sell(s.chain, s.leader, s.token, frac))

    async def run(self):
        for a in self.adapters.values():
            await a.start()
        for p in self.store.load_open(self.paper, chains=self.adapters):   # xchain positions: own manager
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
