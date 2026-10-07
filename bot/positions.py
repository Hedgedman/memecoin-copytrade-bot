from __future__ import annotations
import asyncio
import logging
import time
from .models import Position
from .store import Store
from .chains.base import RateLimited

log = logging.getLogger("positions")


class PositionManager:
    """Marks every open position to its real exit quote and enforces exits.

    PnL is measured on the quote for selling the WHOLE position, so slippage is
    already inside the number your TP/SL reacts to.

    Two triggers: a poll loop (every poll_interval_s) for everything, and on_tick()
    for pool-priced tokens, which re-checks a position the moment its pool changes.
    """

    def __init__(self, cfg: dict, adapters: dict, store: Store):
        self.cfg, self.adapters, self.store = cfg, adapters, store
        self.tp = float(cfg.get("take_profit", 0.20))            # 0 = no fixed take-profit
        # trailing stop: once value is up trailing_activate over cost, sell when it falls
        # trailing_pct below its peak. trailing_pct 0 = off.
        # Pick activate >= 1/(1-trailing_pct) - 1 (e.g. +43% for a 30% trail) so a trade that
        # activated the trail can't end below break-even (apart from gaps).
        self.trail_act = float(cfg.get("trailing_activate", 0.0))
        self.trail_pct = float(cfg.get("trailing_pct", 0.0))
        self.sl = float(cfg.get("stop_loss", 0.04))
        self.max_hold = float(cfg.get("max_hold_s", 600))
        self.poll = float(cfg.get("poll_interval_s", 1.0))
        self.follow_exit = bool(cfg.get("follow_leader_exit", True))
        self.stop_basis = cfg.get("stop_basis", "cost")   # "cost" or "entry_price"
        self.max_quote_failures = int(cfg.get("max_quote_failures", 20))
        self.mark_interval = float(cfg.get("mark_interval_s", 1.0))   # min seconds between saved marks
        self.positions: list[Position] = []
        self._checking: set[int] = set()

    def open(self) -> list[Position]:
        return [p for p in self.positions if p.status != "closed"]

    def _exits(self, p: Position) -> tuple[float, float, float, float, float, bool]:
        """(take_profit, trailing_activate, trailing_pct, stop_loss, max_hold_s, follow_leader_exit):
        a leader profile's exits stored in the position (bot/profiles.py), else the global ones."""
        e = p.timing.get("exits")
        if not e:
            return self.tp, self.trail_act, self.trail_pct, self.sl, self.max_hold, self.follow_exit
        return (float(e.get("take_profit", 0)), float(e.get("trailing_activate", 0)), float(e.get("trailing_pct", 0)),
                float(e.get("stop_loss", self.sl)), float(e.get("max_hold_s", 0)), bool(e.get("follow_leader_exit")))

    def add(self, p: Position):
        self.positions.append(p)
        a = self.adapters.get(p.chain)
        if a is not None and hasattr(a, "track"):
            asyncio.ensure_future(a.track(p.token))

    def on_tick(self, chain: str, token: str):
        """Pool of `token` changed: check matching positions right now (no network call)."""
        for p in self.positions:
            if p.status == "open" and p.chain == chain and p.token == token and id(p) not in self._checking:
                asyncio.ensure_future(self._check(p))

    async def on_leader_sell(self, chain: str, leader: str, token: str):
        for p in self.open():
            if p.chain == chain and p.token == token and p.leader == leader and p.status == "open" \
                    and self._exits(p)[5]:
                await self.close(p, "leader_sold")

    async def close(self, p: Position, reason: str, trigger_value: float | None = None):
        if p.status != "open":
            return
        p.status = "closing"
        if "exit_trigger_at" not in p.timing:      # keep the FIRST trigger across retries
            p.timing["exit_trigger_at"] = time.time()
            p.timing["exit_trigger_value"] = trigger_value
        a = self.adapters[p.chain]
        # escalate slippage on retries: getting out matters more than price
        slip = [None, 3000, 5000, 9000][min(p.sell_attempts, 3)]
        fill = await a.sell(p, slip)
        if not fill.ok:
            if "rate limit" not in (fill.error or ""):
                p.sell_attempts += 1
            p.status = "open"
            log.error("SELL FAILED %s (%s) attempt %d: %s", p.token, reason, p.sell_attempts, fill.error)
            return
        p.status, p.exit_value, p.closed_at = "closed", fill.quote_amount, time.time()
        p.exit_reason, p.exit_sig = reason, fill.signature
        p.timing.update(exit_filled_at=p.closed_at, exit_fill_slot=fill.slot, exit_model=fill.model)
        self.store.close_position(p)
        if hasattr(a, "release"):
            await a.release(p.token)
        pnl = p.exit_value - p.cost
        lag = p.closed_at - p.timing["exit_trigger_at"]
        log.info("CLOSED %s reason=%s pnl=%+.4f (%+.1f%%) held=%.0fs exit_lag=%.2fs", p.token, reason, pnl,
                 100 * pnl / p.cost, p.closed_at - p.opened_at, lag)

    async def _check(self, p: Position):
        key = id(p)
        if key in self._checking:
            return
        self._checking.add(key)
        try:
            await self._check_inner(p)
        finally:
            self._checking.discard(key)

    async def _check_inner(self, p: Position):
        try:
            value = await self.adapters[p.chain].quote_exit(p)
        except RateLimited:
            return          # try again next round; not a sign the token is dead
        if p.status != "open":
            return
        age = time.time() - p.opened_at
        tp, trail_act, trail_pct, sl, max_hold, _ = self._exits(p)
        if value is None:
            p.quote_failures += 1
            if p.quote_failures == self.max_quote_failures or (
                    p.quote_failures > self.max_quote_failures and p.quote_failures % 60 == 0):
                log.error("no exit route for %s after %d tries - likely rugged/illiquid, needs manual check",
                          p.token, p.quote_failures)
            if max_hold > 0 and age > max_hold:
                await self.close(p, "max_hold_no_quote")
            return
        p.quote_failures = 0
        now = time.time()
        if now - p.last_mark_at >= self.mark_interval:
            self.store.mark(p, value)
            p.last_mark_at = now
        if p.ref_value is None:
            p.ref_value = value          # first exit quote after buying = our "entry price"
            self.store.set_ref(p)        # saved, so a restart keeps the same stop level
        p.peak_value = value if p.peak_value is None else max(p.peak_value, value)
        ret = value / p.cost - 1
        stop_ret = value / p.ref_value - 1 if self.stop_basis == "entry_price" else ret
        trailing_on = trail_pct > 0 and p.peak_value / p.cost - 1 >= trail_act
        if tp > 0 and ret >= tp:
            await self.close(p, "take_profit", value)
        elif trailing_on and value <= p.peak_value * (1 - trail_pct):
            await self.close(p, "trailing_stop", value)
        elif not trailing_on and stop_ret <= -sl:
            await self.close(p, "stop_loss", value)
        elif max_hold > 0 and age > max_hold:
            await self.close(p, "max_hold", value)

    async def run(self):
        while True:
            live = [p for p in self.open() if p.status == "open"]
            if live:
                await asyncio.gather(*(self._check(p) for p in live), return_exceptions=True)
            await asyncio.sleep(self.poll)
