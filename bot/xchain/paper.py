"""Cross-chain PAPER trading: mirror the leaders' EVM trades with simulated fills. Never trades.

There is no execution code here: every "fill" is a Relay POST /price quote for OUR size.
One position per (chain, token, leader): each leader's trades are mirrored separately.

Entries   his Relay buy (watcher.record) -> our size = size_mult x his USD, capped at
          max_usd_per_token per token (add-ons included, summed over all leaders holding it),
          skipped under min_usd. Limits: max_open_positions in total, max_open_per_leader. Filled on a quote
          taken entry_delay_s after we detected his trade (our realistic latency), Relay fees included
          (currencyOut is net of relayer fees; origin gas is added to our cost).
Exits     he sells X% of his holding -> we sell X% of ours, quoted token -> Solana USDC.
            Relay sells  : from the watcher (his Relay request).
            DEX sells    : ERC-20 Transfer logs FROM his EVM address for tokens we hold (eth_getLogs,
                           filtered by token address); a transfer with no Relay request = DEX sale.
          X = sold / (sold + his balanceOf after), or from the buys/sells we tracked if no RPC.
          Safety stop: every mark_interval_s a quote for our remaining tokens (also saved as a mark);
          remaining value <= (1 - safety_stop) x their cost -> sell everything.
Storage   positions (chain robinhood/bsc, paper=1, cost/exit_value in USD), marks = realized USD +
          quote for the remaining tokens (comparable with cost), xchain_fills = one row per fill.
          Position.timing holds the running state: remaining, realized_usd, basis_usd, bought_usd,
          his_bought/his_sold, evm, symbol, decimals, chain_id.
          xchain_fills also hold leader + detect_delay_s (how late we saw his trade).
Profiles  a leader with a profile (bot/profiles.py): size = factor x his USD, own cap per token (incl. his
          adds) and minimum. Mode "trail": his Relay/DEX sells are NOT followed; every profile mark_interval_s
          a Relay quote checks the trail (peak of the remaining value, active once +trailing_activate) and
          the stop-loss before that. An add lifts the peak by its cost. Exits: trailing_stop / stop_loss.
          Mode "mirror": his sells are mirrored pro rata like the default leaders; the profile's stop_loss
          replaces the global safety_stop. The exits stored in the position decide, so positions opened
          before a profile existed keep their rules.
H6        (Oct 8 2026) normal (non-profile) leaders get the Solana exits instead of mirroring his sells:
          take_profit / stop_loss / stop_basis from the GLOBAL exits config (TP +30% on cost, stop -20% from the
          first quote after the fill), his sells IGNORED, max_hold_h (48 h: tokenized stocks/gold that move ~0%
          cannot be detected reliably - Relay's currency metadata only carries `verified`), marks every
          normal_mark_interval_s (30 s, stretched when many positions would exceed the Relay budget). Stored per
          position as timing["exits"] = {mode: "fixed", rule: "H6", ...}; positions opened before (no exits
          stamp) keep the old rules: his sells mirrored + safety_stop.
EVM RPC   per chain: the configured URL (env, e.g. Alchemy), the public URL as fallback.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import OrderedDict, defaultdict

from ..models import Position
from .relay import Quote, XTrade, parse_quote, price_body

log = logging.getLogger("xpaper")

TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
pad_addr = lambda a: "0x" + "0" * 24 + a.lower()[2:]


def normal_exits_from(exits_cfg: dict | None, max_hold_h: float = 48.0) -> dict | None:
    """H6 exits for normal leaders from the global exits config (same keys as Solana), or None = legacy rules."""
    if exits_cfg is None:
        return None
    return {"mode": "fixed", "rule": "H6", "take_profit": float(exits_cfg.get("take_profit", 0.30)),
            "stop_loss": float(exits_cfg.get("stop_loss", 0.20)), "stop_basis": exits_cfg.get("stop_basis", "entry_price"),
            "max_hold_s": float(max_hold_h) * 3600, "follow_leader_exit": False, "trailing_activate": 0.0,
            "trailing_pct": 0.0}


class XchainPaperTrader:
    def __init__(self, cfg: dict, store, paper: bool, leaders, kill_switch_file: str = "STOP", profiles=None,
                 global_exits: dict | None = None, watch=None):
        if not paper:
            raise ValueError("xchain trading is paper-only (no execution exists): set paper: true")
        self.watch = set(watch or ())                                        # status watch: no positions
        self.normal_exits = normal_exits_from(global_exits, float(cfg.get("max_hold_h", 48)))
        self.mark_normal_s = float(cfg.get("normal_mark_interval_s", 30))
        self.sol_usd = float(cfg.get("sol_usd_for_limits", 200))   # converts a profile's Solana PnL for daily_loss_usd
        self.store = store
        self.leaders = set(leaders)                                          # Solana leader wallets
        self.profiles = dict(profiles or {})                                 # leader -> Profile (bot/profiles.py)
        self.chains = set(cfg.get("chains", ["robinhood", "bsc"]))
        self.mult = float(cfg.get("size_mult", 0.4))
        self.cap = float(cfg.get("max_usd_per_token", 2000))
        self.min_usd = float(cfg.get("min_usd", 50))
        self.delay = float(cfg.get("entry_delay_s", 15))
        self.max_age = float(cfg.get("max_signal_age_s", 120))
        self.max_open = int(cfg.get("max_open_positions", 50))
        self.max_per_leader = int(cfg.get("max_open_per_leader", 5))
        self.stop = float(cfg.get("safety_stop", 0.60))
        self.mark_s = float(cfg.get("mark_interval_s", 120))
        self.logs_s = float(cfg.get("logs_poll_s", 15))
        self.dex_confirm_s = float(cfg.get("dex_confirm_s", 15))
        self.max_span = int(cfg.get("max_log_span", 5000))
        self.head_lag = int(cfg.get("log_head_lag", 3))          # stay behind the head: load-balanced RPCs lag
        self.fail_warn = int(cfg.get("log_fail_warn", 8))         # warn after this many failed rounds in a row
        self.sell_retries = int(cfg.get("sell_retries", 3))
        self.kill = kill_switch_file
        self.positions: dict[tuple[str, str, str], Position] = {          # (chain, token, leader)
            (p.chain, p.token, p.leader): p for p in store.load_open(True, self.chains) if p.timing.get("xchain")}
        self.locks: dict[tuple[str, str, str], asyncio.Lock] = defaultdict(asyncio.Lock)
        self._opening: set[tuple[str, str, str]] = set()
        self._reserved: dict[tuple[str, str], float] = defaultdict(float)  # (chain, token) -> USD being bought now
        self.client = self.session = None
        self.rpc: dict[str, str] = {}
        self.rpc_fallback: dict[str, str] = {}
        self._tasks: set[asyncio.Task] = set()
        self._last_block: dict[str, int] = {}
        self._seen_logs: OrderedDict[str, None] = OrderedDict()
        self._logs_warned: set[str] = set()
        self._logs_failing: dict[str, int] = {}                   # chain -> failed rounds in a row

    # ---------- inputs ----------
    def on_his_trade(self, t: XTrade, leader: str, evm: str, detected_at: float):
        """Called by the watcher for every NEW Relay request it records. Cheap; never raises."""
        if t.chain not in self.chains or t.status != "success" or leader not in self.leaders:
            return
        evm = evm.lower()
        if t.side == "buy":
            if leader in self.watch:
                self._skip(t, leader, "watch leader")
                return
            self._spawn(self._buy(t, leader, evm, detected_at))
        elif t.side == "sell":
            # decided by the exits stored in OUR position, so positions opened before a profile existed
            # keep mirroring his sells
            p = next((q for k, q in self.positions.items()
                      if k[0] == t.chain and k[1].lower() == t.token.lower() and k[2] == leader), None)
            if p is not None and not self._follows(p):                                   # we exit ourselves
                log.info("xchain %s sold %s on %s: not followed (%s exits on its own rules)",
                         leader[:8], t.symbol, t.chain, self._rule(p))
                return
            his = {"his_ref": t.request_id, "his_ts": t.deposit_ts, "his_usd": t.usd, "his_price_usd": t.price_usd,
                   "detected_at": detected_at}
            self._spawn(self._mirror_sell(t.chain, t.token, leader, evm, t.token_amount, detected_at,
                                          "leader_sold", his))

    @staticmethod
    def _follows(p: Position) -> bool:
        """His sells are mirrored unless the position's stored exits say otherwise (trail / H6 fixed exits)."""
        e = p.timing.get("exits")
        return True if not e else bool(e.get("follow_leader_exit", True))

    @staticmethod
    def _rule(p: Position) -> str:
        return p.timing.get("profile") or (p.timing.get("exits") or {}).get("rule") or "legacy"

    def hot_leaders(self) -> set[str]:
        """Leaders we hold an open paper position of (their sells are polled fastest)."""
        return {k[2] for k in self.positions} | {k[2] for k in self._opening}

    def _spawn(self, coro):
        task = asyncio.ensure_future(self._guard(coro))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    @staticmethod
    async def _guard(coro):
        try:
            await coro
        except asyncio.CancelledError:
            raise
        except Exception as e:                                   # paper side job: never disturb the bot
            log.exception("xchain paper error: %s", e)

    # ---------- entries ----------
    async def _buy(self, t: XTrade, leader: str, evm: str, detected_at: float):
        key = (t.chain, t.token, leader)
        async with self.locks[key]:
            p = self.positions.get(key)
            if p is not None:                                    # his holding, for the no-RPC fallback
                p.timing["his_bought"] = str(int(p.timing.get("his_bought", "0")) + t.token_amount)
            age = detected_at - (t.deposit_ts or detected_at)
            prof = self.profiles.get(leader)
            target = (prof.factor if prof else self.mult) * t.usd
            tkey = (t.chain, t.token.lower())
            in_token = self._reserved[tkey] + sum(              # all leaders together, incl. buys in flight
                q.timing["bought_usd"] for (c, tk, _), q in self.positions.items()
                if c == t.chain and tk.lower() == tkey[1])
            size = min(target, self.cap - in_token)
            prof_why = None
            if prof is not None:                                 # own cap per token (incl. his adds) + minimum
                own, prof_why = prof.size_usd(t.usd, p.timing["bought_usd"] if p else 0.0)
                size = min(size, own)
            # the global limits count NORMAL positions only; a profile has its own (max_open_tokens over
            # Solana + xchain via the store, daily_loss_usd)
            normal = [k for k, q in self.positions.items() if not q.timing.get("profile")] + [
                k for k in self._opening if self.profiles.get(k[2]) is None]
            n_leader = sum(k[2] == leader for k in normal)
            why = None
            if age > self.max_age:
                why = f"stale: detected {age:.0f}s after his trade"
            elif os.path.exists(self.kill):
                why = "kill switch file present"
            elif prof is None and p is None and len(normal) >= self.max_open:
                why = f"max open xchain positions ({self.max_open})"
            elif prof is None and p is None and n_leader >= self.max_per_leader:
                why = f"max open xchain positions per leader ({self.max_per_leader})"
            elif prof_why:
                why = prof_why
            elif prof is not None and p is None:
                why = self._profile_can_open(prof)
            elif size < (prof.min_usd if prof else self.min_usd):
                why = (f"cap ${self.cap:,.0f}/token reached" if target >= self.min_usd or prof
                       else f"our size ${target:,.2f} < ${self.min_usd:,.0f}")
            if why:
                self._skip(t, leader, why)
                if p is not None:
                    self.store.update_position(p)
                return
            if p is None:
                self._opening.add(key)
            self._reserved[tkey] += size
            try:
                await self._sleep_until(detected_at + self.delay)
                q = await self._quote("buy", leader, evm, t.chain_id, t.token, int(size * 1e6), priority=0)
                now = time.time()
                if q is None:
                    self._skip(t, leader, "no Relay route for our size")
                    return
                cost = size + q.gas_usd
                if p is None:
                    p = Position(None, t.chain, t.token, leader, q.out_raw, cost, now, t.request_id, True, timing={
                        "xchain": 1, "symbol": t.symbol, "decimals": t.decimals, "chain_id": t.chain_id,
                        "evm": evm, "remaining": str(q.out_raw), "realized_usd": 0.0, "basis_usd": cost,
                        "bought_usd": size, "his_bought": str(t.token_amount), "his_sold": "0"})
                    if prof is not None:
                        p.timing.update(prof.stamp(), **({} if prof.mirrors else {"peak_value": cost}))
                    elif self.normal_exits is not None:                      # H6: Solana exits, his sells ignored
                        p.timing["exits"] = dict(self.normal_exits)
                    p.id = self.store.open_position(p)
                    self.positions[key] = p
                    kind = "open"
                else:
                    p.token_amount += q.out_raw
                    p.cost += cost
                    p.timing["remaining"] = str(int(p.timing["remaining"]) + q.out_raw)
                    p.timing["basis_usd"] += cost
                    p.timing["bought_usd"] += size
                    if "peak_value" in p.timing:                 # trail: the add's cost lifts the peak too,
                        p.timing["peak_value"] += cost           # so adding at a higher price can't trip it
                    if p.timing.get("ref_price_usd"):            # H6 stop reference: weighted with the add's price
                        old_rem = int(p.timing["remaining"]) - q.out_raw
                        p.timing["ref_price_usd"] = (p.timing["ref_price_usd"] * old_rem + cost / (q.out_raw / 10 ** t.decimals)
                                                     * q.out_raw) / int(p.timing["remaining"])
                    self.store.update_position(p)
                    kind = "add"
            finally:
                self._opening.discard(key)
                self._reserved[tkey] -= size
                if self._reserved[tkey] <= 1e-9:
                    del self._reserved[tkey]
            our_price = cost / (q.out_raw / 10 ** t.decimals)
            self._fill(p, "buy", kind, 1.0, now, q, cost, q.out_raw, our_price,
                       {"his_ref": t.request_id, "his_ts": t.deposit_ts, "his_usd": t.usd,
                        "his_price_usd": t.price_usd, "detected_at": detected_at})

    def _profile_can_open(self, prof) -> str | None:
        n = self.store.profile_open_count(prof.name) + sum(k[2] == prof.leader for k in self._opening)
        if n >= prof.max_open_tokens:
            return f"profile {prof.name}: max open tokens ({prof.max_open_tokens})"
        lost = self.store.profile_pnl_today_usd(prof.name, self.sol_usd)
        if lost <= -prof.daily_loss_usd:
            return f"profile {prof.name}: daily loss limit ${prof.daily_loss_usd:,.0f} hit (${lost:,.0f})"
        return None

    # ---------- exits ----------
    async def _mirror_sell(self, chain: str, token: str, leader: str, evm: str, sold_raw: int,
                           detected_at: float, reason: str, his: dict):
        key = (chain, token, leader)
        async with self.locks[key]:
            p = self.positions.get(key)
            if p is None:
                return
            frac, how = await self._his_fraction(p, evm, sold_raw)
            p.timing["his_sold"] = str(int(p.timing.get("his_sold", "0")) + sold_raw)
            rem = int(p.timing["remaining"])
            amount = rem if frac >= 0.99 else int(rem * frac)
            log.info("xchain he sold %.1f%% of his %s on %s (%s, %s) -> we sell %.1f%% of ours",
                     frac * 100, p.timing.get("symbol"), chain, reason, how, amount / rem * 100 if rem else 0)
            if amount <= 0:
                return
            await self._sleep_until(detected_at + self.delay)
            await self._sell(p, amount, reason, frac, his)

    async def _his_fraction(self, p: Position, evm: str, sold_raw: int) -> tuple[float, str]:
        bal = await self._balance(p.chain, p.token, evm)
        if bal is not None:
            return (min(1.0, sold_raw / (sold_raw + bal)) if sold_raw + bal else 1.0), "balanceOf"
        held = int(p.timing.get("his_bought", "0")) - int(p.timing.get("his_sold", "0"))
        return (min(1.0, sold_raw / held) if held > 0 else 1.0), "tracked"

    async def _sell(self, p: Position, amount: int, reason: str, frac: float, his: dict) -> bool:
        """Paper-sell `amount` raw tokens of p at a Relay quote. Must hold self.locks[p]."""
        if p.status != "open":
            return False
        evm, d = p.timing["evm"], int(p.timing["decimals"])
        q = None
        for attempt in range(self.sell_retries):
            q = await self._quote("sell", p.leader, evm, int(p.timing["chain_id"]), p.token, amount, priority=0)
            if q is not None:
                break
            await asyncio.sleep(min(20.0, self.mark_s))
        if q is None:
            log.error("xchain paper SELL %s %s: no Relay route after %d tries - position stays open",
                      p.timing.get("symbol"), reason, self.sell_retries)
            self.store.record_event(time.time(), p.leader, "", "xchain_paper_sell_failed", p.token, None,
                                    f"{p.chain} {reason}: no Relay quote for {amount} raw")
            return False
        now = time.time()
        rem = int(p.timing["remaining"])
        proceeds = q.out_ui - q.gas_usd                          # USDC out, minus EVM gas
        p.timing["basis_usd"] -= p.timing["basis_usd"] * amount / rem
        p.timing["remaining"] = str(rem - amount)
        p.timing["realized_usd"] += proceeds
        our_price = proceeds / (amount / 10 ** d)
        if rem - amount <= 0:
            p.status, p.exit_value, p.closed_at = "closed", p.timing["realized_usd"], now
            p.exit_reason, p.exit_sig = reason, his.get("his_ref") or ""
            p.timing.update(exit_filled_at=now)
            self.store.close_position(p)
            self.positions.pop((p.chain, p.token, p.leader), None)
            kind = "close"
        else:
            self.store.update_position(p)
            kind = "partial"
        self._fill(p, "sell", f"{kind} {reason}", frac, now, q, proceeds, amount, our_price, his)
        if kind == "close":
            pnl = p.exit_value - p.cost
            log.info("xchain paper CLOSED %s %s reason=%s pnl $%+.2f (%+.1f%%) held %.1fh", p.chain,
                     p.timing.get("symbol"), reason, pnl, 100 * pnl / p.cost, (now - p.opened_at) / 3600)
        return True

    # ---------- marks + safety stop / profile trail ----------
    def _mark_interval(self, p: Position) -> float:
        prof = self.profiles.get(p.leader)
        if prof is not None and p.timing.get("profile") == prof.name:
            return prof.mark_interval_s
        if (p.timing.get("exits") or {}).get("rule") == "H6":
            # 30 s per position; with many H6 positions the marks alone would exceed the Relay budget
            # (100 calls/min, marks get the 0.7 share): stretch so marks use at most ~60% of it
            n = sum((q.timing.get("exits") or {}).get("rule") == "H6" for q in self.positions.values())
            budget = getattr(getattr(self.client, "limiter", None), "per_window", 100)
            return max(self.mark_normal_s, n * 60.0 / (0.6 * budget))
        return self.mark_s

    async def mark_once(self, due_only: bool = False):
        """Relay quote for each position's remaining tokens -> mark + exit checks. due_only: only positions
        whose own mark interval has passed (profile positions are marked more often)."""
        now = time.time()
        for p in list(self.positions.values()):
            if self.locks[(p.chain, p.token, p.leader)].locked():
                continue                                         # a buy/sell is in progress
            if due_only and now - p.last_mark_at < self._mark_interval(p):
                continue
            p.last_mark_at = now
            rem = int(p.timing["remaining"])
            q = await self._quote("sell", p.leader, p.timing["evm"], int(p.timing["chain_id"]), p.token, rem,
                                  priority=2)
            if q is None:
                continue
            value_rem = q.out_ui - q.gas_usd
            self.store.mark(p, p.timing["realized_usd"] + value_rem)
            basis = p.timing["basis_usd"]
            exits = p.timing.get("exits")
            if exits and exits.get("mode") == "fixed":           # H6: TP on cost, stop from the first quote, max hold
                units = rem / 10 ** int(p.timing["decimals"])
                price = value_rem / units if units else 0.0
                if not p.timing.get("ref_price_usd"):
                    p.timing["ref_price_usd"] = price            # first quote after the fill = stop reference
                    self.store.update_position(p)
                total = p.timing["realized_usd"] + value_rem
                ret = total / p.cost - 1
                ref = p.timing["ref_price_usd"] if exits.get("stop_basis", "entry_price") == "entry_price" else basis / units
                stop_ret = price / ref - 1 if ref else 0.0
                tp, sl, max_hold = float(exits.get("take_profit", 0)), float(exits["stop_loss"]), float(exits.get("max_hold_s", 0))
                why = ("take_profit" if tp > 0 and ret >= tp else "stop_loss" if stop_ret <= -sl
                       else "max_hold" if max_hold > 0 and now - p.opened_at > max_hold else None)
                if why:
                    log.info("xchain %s %s %s (H6): total $%.2f vs cost $%.2f (%+.0f%%), price vs first quote %+.0f%%, "
                             "held %.1fh", why.upper(), p.chain, p.timing.get("symbol"), total, p.cost, 100 * ret,
                             100 * stop_ret, (now - p.opened_at) / 3600)
                    self._spawn(self._stop(p, time.time(), why))
            elif exits and exits.get("mode") == "mirror_ladder":  # mirror + own ladder + safety stop on the rest
                units = rem / 10 ** int(p.timing["decimals"])
                price, avg = (value_rem / units if units else 0.0), (basis / units if units else 0.0)
                fired = list(p.timing.get("ladder_fired") or [])
                due = [lv for lv in sorted(float(x) for x in exits.get("ladder_levels") or [])
                       if lv not in fired and avg and price >= lv * avg]
                if due:
                    self._spawn(self._ladder(p, due, float(exits.get("ladder_pct", 0.25)), price, avg))
                elif value_rem <= (1 - float(exits["stop_loss"])) * basis:
                    log.warning("xchain SAFETY STOP %s %s (%s): remaining worth $%.2f vs cost $%.2f (%+.0f%%)",
                                p.chain, p.timing.get("symbol"), p.timing.get("profile"), value_rem, basis,
                                100 * (value_rem / basis - 1))
                    self._spawn(self._stop(p, time.time()))
            elif exits and exits.get("mode") == "trail":         # leader profile: we exit ourselves
                peak = max(p.timing.get("peak_value", basis), value_rem)
                if peak != p.timing.get("peak_value"):
                    p.timing["peak_value"] = peak
                    self.store.update_position(p)
                active = peak >= basis * (1 + float(exits["trailing_activate"]))
                if active and value_rem <= peak * (1 - float(exits["trailing_pct"])):
                    log.info("xchain TRAIL %s %s (%s): $%.2f is %.0f%% below its peak $%.2f (cost $%.2f)", p.chain,
                             p.timing.get("symbol"), p.timing.get("profile"), value_rem, 100 * (1 - value_rem / peak),
                             peak, basis)
                    self._spawn(self._stop(p, time.time(), "trailing_stop"))
                elif not active and value_rem <= basis * (1 - float(exits["stop_loss"])):
                    log.info("xchain STOP %s %s (%s): remaining worth $%.2f vs cost $%.2f (%+.0f%%)", p.chain,
                             p.timing.get("symbol"), p.timing.get("profile"), value_rem, basis,
                             100 * (value_rem / basis - 1))
                    self._spawn(self._stop(p, time.time(), "stop_loss"))
            elif value_rem <= (1 - float((exits or {}).get("stop_loss", self.stop))) * basis:
                log.warning("xchain SAFETY STOP %s %s%s: remaining worth $%.2f vs cost $%.2f (%+.0f%%)",
                            p.chain, p.timing.get("symbol"),
                            f" ({p.timing['profile']})" if p.timing.get("profile") else "",
                            value_rem, basis, 100 * (value_rem / basis - 1))
                self._spawn(self._stop(p, time.time()))

    async def _ladder(self, p: Position, levels: list[float], pct: float, price: float, avg: float):
        """Sell pct of what we still hold at each due ladder level, once per level (timing["ladder_fired"])."""
        async with self.locks[(p.chain, p.token, p.leader)]:
            for level in levels:
                if (p.chain, p.token, p.leader) not in self.positions:
                    return
                fired = list(p.timing.get("ladder_fired") or [])
                if level in fired:
                    continue
                rem = int(p.timing["remaining"])
                log.info("xchain LADDER %s %s (%s): %gx our average entry ($%.4g vs $%.4g) -> selling %.0f%% of the "
                         "holding", p.chain, p.timing.get("symbol"), p.timing.get("profile"), level, price, avg, 100 * pct)
                if await self._sell(p, max(1, int(rem * pct)), f"ladder_{level:g}x", pct, {}):
                    p.timing["ladder_fired"] = fired + [level]
                    if p.status == "open":
                        self.store.update_position(p)
                    else:
                        self.store.close_position(p)

    async def _stop(self, p: Position, triggered_at: float, reason: str = "safety_stop"):
        async with self.locks[(p.chain, p.token, p.leader)]:
            if (p.chain, p.token, p.leader) not in self.positions:
                return
            await self._sleep_until(triggered_at + self.delay)
            await self._sell(p, int(p.timing["remaining"]), reason, 1.0, {})

    # ---------- DEX sells (ERC-20 Transfer logs out of his EVM address) ----------
    async def logs_once(self):
        for chain in self.chains:
            held = {p.token.lower() for p in self.positions.values() if p.chain == chain}
            if not held:
                self._last_block.pop(chain, None)
                continue
            if not (self.rpc.get(chain) or self.rpc_fallback.get(chain)):
                self._warn_logs(chain, "no RPC configured")
                continue
            evms = sorted({p.timing["evm"] for p in self.positions.values() if p.chain == chain})
            latest, logs = None, None
            for attempt in range(2):                             # one quick retry (e.g. a node behind the head)
                if attempt:
                    await asyncio.sleep(1.0)
                head = await self._rpc(chain, "eth_blockNumber", [])
                if not isinstance(head, str):
                    logs = head
                    continue
                latest = int(head, 16) - self.head_lag
                frm = max(self._last_block.get(chain, latest - self.max_span) + 1, latest - self.max_span, 0)
                if frm > latest:
                    logs = []
                    break
                logs = await self._rpc(chain, "eth_getLogs", [{"fromBlock": hex(frm), "toBlock": hex(latest),
                                                             "address": sorted(held),
                                                             "topics": [TRANSFER, [pad_addr(e) for e in evms]]}])
                if isinstance(logs, list):
                    break
            if not isinstance(logs, list):
                # the block cursor stays put: the next round asks for the same range again, nothing is lost
                self._logs_failed(chain, str(logs)[:150])
                continue
            self._logs_ok(chain)
            self._last_block[chain] = max(self._last_block.get(chain, 0), latest)
            for lg in logs:
                k = f"{lg['transactionHash']}:{lg.get('logIndex')}"
                if k in self._seen_logs:
                    continue
                self._seen_logs[k] = None
                if len(self._seen_logs) > 5000:
                    self._seen_logs.popitem(last=False)
                self._spawn(self._dex_check(chain, lg, time.time()))

    def _logs_failed(self, chain: str, why: str):
        n = self._logs_failing.get(chain, 0) + 1
        self._logs_failing[chain] = n
        if n == self.fail_warn:
            log.warning("xchain: DEX-sell logs on %s failing for %d rounds in a row (%s) - DEX sells are delayed, "
                        "retrying every round", chain, n, why)
            self.store.record_event(time.time(), "", "", "xchain_logs_failing", "", None, f"{chain}: {n} rounds: {why}")
        else:
            log.debug("xchain: eth_getLogs on %s failed (%d in a row, will retry): %s", chain, n, why)

    def _logs_ok(self, chain: str):
        n = self._logs_failing.pop(chain, 0)
        if n >= self.fail_warn:
            log.info("xchain: DEX-sell logs on %s working again after %d failed rounds (range re-queried)", chain, n)

    def _warn_logs(self, chain: str, why: str):
        if chain not in self._logs_warned:
            self._logs_warned.add(chain)
            log.warning("xchain: DEX sells on %s are NOT detected (%s) - only Relay sells are mirrored", chain, why)
            self.store.record_event(time.time(), "", "", "xchain_logs_unavailable", "", None, f"{chain}: {why}")

    async def _dex_check(self, chain: str, lg: dict, detected_at: float):
        """A token left his EVM wallet. Relay request with that deposit tx -> the Relay path handles it;
        otherwise he sold on a DEX (or moved it away): mirror it."""
        tx, token = lg["transactionHash"], lg["address"].lower()
        evm = "0x" + lg["topics"][1][-40:]
        amount = int(lg["data"], 16) if lg.get("data") not in (None, "0x") else 0
        await asyncio.sleep(self.dex_confirm_s)
        if self.store.xtrade_has_deposit(tx) or await self.client.requests(priority=1, depositTxHash=tx):
            return
        held = [q for (c, t, _), q in self.positions.items()
                if c == chain and t.lower() == token and q.timing.get("evm", "").lower() == evm]
        if not held or amount <= 0:
            return
        blk = await self._rpc(chain, "eth_getBlockByNumber", [lg["blockNumber"], False])
        his_ts = int(blk["timestamp"], 16) if isinstance(blk, dict) and blk.get("timestamp") else None
        for p in held:
            if not self._follows(p):
                log.info("xchain DEX sell by %s (%s) on %s: not followed (%s)", evm[:10], p.leader[:8],
                         chain, self._rule(p))
                continue
            log.info("xchain DEX sell by %s (%s) on %s: %s raw %s (tx %s)", evm[:10], p.leader[:8], chain, amount,
                     p.timing.get("symbol"), tx[:12])
            self.store.record_event(time.time(), p.leader, tx, "xchain_dex_sell", p.token, None,
                                    f"{chain}: {amount / 10 ** int(p.timing['decimals']):,.6g} "
                                    f"{p.timing.get('symbol')} out of {evm[:10]} without a Relay request")
            await self._mirror_sell(chain, p.token, p.leader, evm, amount, detected_at, "leader_sold_dex",
                                    {"his_ref": tx, "his_ts": his_ts, "his_usd": None, "his_price_usd": None,
                                     "detected_at": detected_at})

    # ---------- helpers ----------
    def _fill(self, p: Position, side: str, reason: str, frac: float, now: float, q: Quote, usd: float,
              tokens: int, our_price: float, his: dict):
        hp = his.get("his_price_usd")
        gap = (our_price / hp - 1) * 100 if hp and our_price else None
        delay = now - his["his_ts"] if his.get("his_ts") else None
        seen = his["detected_at"] - his["his_ts"] if his.get("his_ts") and his.get("detected_at") else None
        self.store.record_xfill(ts=now, position_id=p.id, chain=p.chain, token=p.token,
                                symbol=p.timing.get("symbol"), side=side, reason=reason, fraction=frac,
                                our_usd=usd, our_tokens=tokens, our_price_usd=our_price, gap_pct=gap,
                                delay_s=delay, impact_pct=q.impact_pct, gas_usd=q.gas_usd, leader=p.leader,
                                detect_delay_s=seen, **his)
        log.info("xchain paper %s %s %s %s: $%.2f for %s %s | ours $%.4g vs his %s (%s) | seen %s, filled %s "
                 "after him | impact %s%%", side.upper(), reason, p.chain, p.leader[:8], usd,
                 f"{tokens / 10 ** int(p.timing['decimals']):,.6g}", p.timing.get("symbol"), our_price,
                 f"${hp:.4g}" if hp else "n/a (DEX)", f"{gap:+.1f}%" if gap is not None else "-",
                 f"{seen:.1f}s" if seen is not None else "-", f"{delay:.1f}s" if delay is not None else "-",
                 q.impact_pct)

    def _skip(self, t: XTrade, leader: str, why: str):
        log.info("xchain paper skip %s %s $%.2f: %s", t.chain, t.symbol, t.usd, why)
        self.store.record_event(time.time(), leader, t.deposit_tx, "xchain_paper_skip", t.token, None,
                                f"{t.chain} {t.symbol} his ${t.usd:,.2f}: {why}")

    async def _quote(self, side, sol_user, evm, chain_id, token, amount_raw, priority: int = 1) -> Quote | None:
        if amount_raw <= 0 or self.client is None:
            return None
        body = price_body(side, sol_user, evm, chain_id, token, amount_raw)
        return parse_quote(await self.client.price(body, priority=priority) or {})

    async def _balance(self, chain: str, token: str, evm: str) -> int | None:
        r = await self._rpc(chain, "eth_call", [{"to": token, "data": "0x70a08231" + pad_addr(evm)[2:]}, "latest"])
        try:
            return int(r, 16)
        except (TypeError, ValueError):
            return None

    async def _rpc(self, chain: str, method: str, params: list):
        """JSON-RPC on the chain's RPC; the public fallback if it fails (no answer or an error)."""
        r = None
        for url in (self.rpc.get(chain), self.rpc_fallback.get(chain)):
            if not url:
                continue
            r = await self._eth(url, method, params)
            if r is not None and not (isinstance(r, dict) and "code" in r):
                return r
        return r

    async def _eth(self, url: str, method: str, params: list):
        """JSON-RPC call -> result, or the error dict / None. Never raises."""
        try:
            async with self.session.post(url, json={"jsonrpc": "2.0", "id": 1, "method": method,
                                                    "params": params}) as r:
                j = await r.json(content_type=None)
            return j.get("result") if "result" in j else j.get("error")
        except Exception as e:
            log.debug("%s on %s failed: %s", method, url.split("//")[-1].split("/")[0], type(e).__name__)
            return None

    @staticmethod
    async def _sleep_until(at: float):
        await asyncio.sleep(max(0.0, at - time.time()))

    async def run(self, client, session, rpc: dict, rpc_fallback: dict | None = None):
        self.client, self.session, self.rpc = client, session, dict(rpc)
        self.rpc_fallback = dict(rpc_fallback or {})
        log.info("xchain PAPER trading on %s for %d leaders: %.2fx his size, cap $%.0f/token, min $%.0f, delay "
                 "%.0fs, stop -%.0f%%, max %d open / %d per leader (%d resumed); DEX-sell logs on %s",
                 sorted(self.chains), len(self.leaders), self.mult, self.cap, self.min_usd, self.delay,
                 self.stop * 100, self.max_open, self.max_per_leader, len(self.positions),
                 [c for c in sorted(self.chains) if self.rpc.get(c) or self.rpc_fallback.get(c)] or "none")
        if self.normal_exits is not None:
            e = self.normal_exits
            log.info("xchain H6 exits for normal leaders: TP +%.0f%% on cost, stop -%.0f%% from the first quote "
                     "(%s), max hold %.0fh, his sells ignored, marks every %.0fs (%d open H6 / %d legacy positions)",
                     100 * e["take_profit"], 100 * e["stop_loss"], e["stop_basis"], e["max_hold_s"] / 3600,
                     self.mark_normal_s, sum(self._rule(p) == "H6" for p in self.positions.values()),
                     sum(self._rule(p) == "legacy" for p in self.positions.values()))
        for p in self.positions.values():                         # first marks ~10 s after start
            p.last_mark_at = time.time() - self._mark_interval(p) + 10
        for leader, prof in self.profiles.items():
            log.info("xchain profile %s for %s: %s, cap $%.0f/token incl. adds, max %d open tokens, daily loss $%.0f, "
                     "marks every %.0fs", prof.name, leader[:8],
                     f"his sells mirrored pro rata + ladder {100 * prof.ladder_pct:.0f}% at "
                     f"{'/'.join(f'{x:g}' for x in prof.ladder_levels)}x, safety stop -{100 * prof.stop_loss:.0f}%"
                     if prof.mode == "mirror_ladder" else
                     f"his sells mirrored pro rata, safety stop -{100 * prof.stop_loss:.0f}%" if prof.mirrors
                     else "own trail/stop, his sells not followed", prof.max_usd_per_token, prof.max_open_tokens,
                     prof.daily_loss_usd, prof.mark_interval_s)
        while True:
            try:
                await self.logs_once()
                await self.mark_once(due_only=True)                  # each position on its own interval
            except Exception as e:
                log.warning("xchain paper loop error: %s", e)
            await asyncio.sleep(self.logs_s)
