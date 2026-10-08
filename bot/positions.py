from __future__ import annotations
import asyncio
import logging
import time
from dataclasses import replace
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
        # source switch (pool -> Jupiter after graduation, pool dropped, correction factor): a first quote from
        # a new source that differs > switch_max_ratio x from the last mark is NOT acted on; the new source
        # must deliver a second quote >= switch_confirm_s later first. ref_value is never rebased.
        self.switch_ratio = float(cfg.get("switch_max_ratio", 2.0))
        self.switch_confirm = float(cfg.get("switch_confirm_s", 2.0))
        self.positions: list[Position] = []
        self._checking: set[int] = set()
        log.info("exits: take_profit %s, stop_loss %s (stop_basis %s), trailing %s, max_hold_s %s, "
                 "follow_leader_exit %s, poll %.2fs",
                 f"+{100 * self.tp:.0f}% on cost" if self.tp > 0 else "off", f"-{100 * self.sl:.0f}%", self.stop_basis,
                 f"{100 * self.trail_pct:.0f}% after +{100 * self.trail_act:.0f}%" if self.trail_pct > 0 else "off",
                 f"{self.max_hold:g}" if self.max_hold > 0 else "off", self.follow_exit, self.poll)

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

    async def on_leader_sell(self, chain: str, leader: str, token: str, fraction: float = 1.0):
        """fraction = the share of HIS holding he sold (1.0 = all). A position with a mirror profile
        (bot/profiles.py) sells the same share of ours; any other position that follows the leader closes."""
        for p in self.open():
            if p.chain == chain and p.token == token and p.leader == leader and p.status == "open" \
                    and self._exits(p)[5]:
                if (p.timing.get("exits") or {}).get("mode") in ("mirror", "mirror_ladder") and fraction < 0.99:
                    await self.partial_sell(p, fraction, "leader_sold")
                else:
                    await self.close(p, "leader_sold")

    async def partial_sell(self, p: Position, fraction: float, reason: str):
        """Sell `fraction` of what we still hold. The proceeds accumulate in timing["realized"] (native units),
        so marks, stops and the final PnL keep comparing the WHOLE position with its cost."""
        amount = int(p.token_amount * fraction)
        if p.status != "open" or amount <= 0:
            return
        p.status = "closing"
        a = self.adapters[p.chain]
        fill = None
        for attempt in range(3):                      # his sell comes once: don't lose it to a rate limit
            fill = await a.sell(replace(p, token_amount=amount), None)
            if fill.ok:
                break
            await asyncio.sleep(2.0)
        if not fill.ok:
            p.status = "open"
            log.error("PARTIAL SELL FAILED %s (%s): %s", p.token, reason, fill.error)
            return
        p.timing.setdefault("initial_amount", p.token_amount)
        p.token_amount -= amount
        p.timing["realized"] = float(p.timing.get("realized", 0.0)) + fill.quote_amount
        p.timing["partial_sells"] = int(p.timing.get("partial_sells", 0)) + 1
        p.status = "open"
        self.store.update_position(p)
        log.info("PARTIAL SELL %s reason=%s sold %.1f%% of what we held for %.4f | realized %.4f of cost %.4f, "
                 "%.1f%% of the position left", p.token, reason, 100 * fraction, fill.quote_amount,
                 p.timing["realized"], p.cost, 100 * p.token_amount / int(p.timing["initial_amount"]))

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
        p.status, p.closed_at = "closed", time.time()
        p.exit_value = float(p.timing.get("realized", 0.0)) + fill.quote_amount   # + earlier partial sells
        p.exit_reason, p.exit_sig = reason, fill.signature
        p.timing.update(exit_filled_at=p.closed_at, exit_fill_slot=fill.slot, exit_model=fill.model)
        self.store.close_position(p)
        if hasattr(a, "release") and not any(q.chain == p.chain and q.token == p.token and q.status != "closed"
                                             for q in self.positions):     # another position may still need the pool
            await a.release(p.token)
        pnl = p.exit_value - p.cost
        lag = p.closed_at - p.timing["exit_trigger_at"]
        log.info("CLOSED %s reason=%s pnl=%+.4f (%+.1f%%) held=%.0fs exit_lag=%.2fs", p.token, reason, pnl,
                 100 * pnl / p.cost, p.closed_at - p.opened_at, lag)
        model = p.timing.get("exit_trigger_value")
        if not p.paper and reason == "stop_loss" and model:
            # the paper model books the stop at the quote that triggered it (pool maths x correction, or Jupiter);
            # the live fill is what the swap really returned: the gap is the live-vs-paper slippage of this stop
            realized = float(p.timing.get("realized", 0.0)) + model
            gap = 100 * (p.exit_value - realized) / p.cost
            log.warning("LIVE STOP %s: filled %.4f vs model %.4f at trigger (%+.1f points of cost, %+.1f%% vs model), "
                        "exit lag %.2fs, attempts %d", p.token, p.exit_value, realized, gap,
                        100 * (p.exit_value / realized - 1), lag, p.sell_attempts + 1)

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
        # after partial sells (mirror profile): `value` is the rest; held = share of the position still held,
        # total = rest + realized, comparable with cost. Untouched positions: held 1, realized 0.
        held = p.token_amount / int(p.timing.get("initial_amount") or p.token_amount)
        total = float(p.timing.get("realized", 0.0)) + value
        if now - p.last_mark_at >= self.mark_interval:
            self.store.mark(p, total)
            p.last_mark_at = now
        if not self._switch_confirmed(p, total, now):
            return                       # unconfirmed first quote of a new source: mark saved, no exit decision
        if p.ref_value is None:
            p.ref_value = value / held   # first exit quote after buying = our "entry price" (whole position)
            self.store.set_ref(p)        # saved, so a restart keeps the same stop level
        p.peak_value = total if p.peak_value is None else max(p.peak_value, total)
        ret = total / p.cost - 1
        e = p.timing.get("exits") or {}
        basis = e.get("stop_basis", self.stop_basis)        # profile positions may stop on cost instead of ref
        stop_ret = value / (p.ref_value if basis == "entry_price" else p.cost) / held - 1
        trailing_on = trail_pct > 0 and p.peak_value / p.cost - 1 >= trail_act
        if e.get("mode") == "mirror_ladder" and await self._ladder(p, value, e):
            return                                          # sold a ladder step: re-quote before any stop
        if tp > 0 and ret >= tp:
            await self.close(p, "take_profit", value)
        elif trailing_on and value <= p.peak_value * (1 - trail_pct):
            await self.close(p, "trailing_stop", value)
        elif not trailing_on and stop_ret <= -sl:
            await self.close(p, "stop_loss", value)
        elif max_hold > 0 and age > max_hold:
            await self.close(p, "max_hold", value)

    async def _ladder(self, p: Position, value: float, e: dict) -> bool:
        """mirror_ladder profile: at each level x our average entry (cost / all tokens ever held, adds included)
        sell ladder_pct of what we STILL hold, once per level (timing["ladder_fired"]). True if something sold."""
        initial = int(p.timing.get("initial_amount") or p.token_amount)
        avg = p.cost / initial if initial else 0.0
        price = value / p.token_amount if p.token_amount else 0.0
        fired, sold = list(p.timing.get("ladder_fired") or []), False
        for level in sorted(float(x) for x in e.get("ladder_levels") or []):
            if level in fired or not avg or price < level * avg:
                continue
            before = p.token_amount
            await self.partial_sell(p, float(e.get("ladder_pct", 0.25)), f"ladder_{level:g}x")
            if p.token_amount == before:                    # sell failed: try again on the next quote
                break
            fired.append(level)
            p.timing["ladder_fired"] = fired
            self.store.update_position(p)
            sold = True
            log.info("LADDER %s: %gx our average entry (%.3g vs %.3g SOL/raw) -> sold %.0f%% of the holding, "
                     "levels done %s", p.token, level, price, avg, 100 * float(e.get("ladder_pct", 0.25)), fired)
            if p.status != "open":
                break
        return sold

    def _switch_confirmed(self, p: Position, total: float, now: float) -> bool:
        """Source-switch guard. Tracks where the quote came from (adapter.quote_source, if it has one). When the
        source changes and the first new quote is > switch_max_ratio away from the last value, the position is
        flagged (timing["source_switch"], timing["flagged"]) and exits wait for a second quote from the same
        source >= switch_confirm_s later. Returns False while waiting. Updates p.last_value."""
        get = getattr(self.adapters.get(p.chain), "quote_source", None)
        src = get(p.token) if get else None
        last_src, last_val = p.timing.get("quote_source"), p.last_value
        p.last_value = total
        if src is None:
            return True
        if src != last_src:
            p.timing["quote_source"] = src
            if last_src is not None and last_val:
                ratio = total / last_val
                if ratio > self.switch_ratio or ratio < 1 / self.switch_ratio:
                    p.pending_switch = (src, now)
                    p.timing["source_switch"] = round(ratio, 4)
                    p.timing["flagged"] = f"source switch {last_src}->{src} x{ratio:.3f} at {now:.0f}"
                    log.warning("%s: price source %s -> %s, first quote %.3fx the last mark (%.4f -> %.4f): "
                                "flagged, waiting %.0fs for a confirming quote", p.token, last_src, src, ratio,
                                last_val, total, self.switch_confirm)
                else:
                    p.pending_switch = None
                    log.info("%s: price source %s -> %s (%.3fx the last mark)", p.token, last_src, src, ratio)
            self.store.update_position(p)
        if p.pending_switch:
            psrc, since = p.pending_switch
            if src != psrc:
                p.pending_switch = (src, now)
                return False
            if now - since < self.switch_confirm:
                return False
            p.pending_switch = None
            log.info("%s: source %s confirmed after %.1fs, acting on %.4f (last pre-switch mark %.4f)", p.token, src,
                     now - since, total, last_val)
        return True

    async def run(self):
        while True:
            live = [p for p in self.open() if p.status == "open"]
            if live:
                await asyncio.gather(*(self._check(p) for p in live), return_exceptions=True)
            await asyncio.sleep(self.poll)
