"""Cross-chain PAPER trading (phase 2): mirror a leader's EVM trades with simulated fills. Never trades.

There is no execution code here: every "fill" is a Relay POST /price quote for OUR size.

Entries   his Relay buy (watcher.record) -> our size = size_mult x his USD, capped at
          max_usd_per_token per token (add-ons included), skipped under min_usd. Filled on a quote
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


class XchainPaperTrader:
    def __init__(self, cfg: dict, store, paper: bool, leaders: dict[str, str], kill_switch_file: str = "STOP"):
        if not paper:
            raise ValueError("xchain trading is paper-only (no execution exists): set paper: true")
        self.store = store
        self.leaders = {k: v.lower() for k, v in leaders.items()}           # solana wallet -> evm address
        self.chains = set(cfg.get("chains", ["robinhood", "bsc"]))
        self.mult = float(cfg.get("size_mult", 0.4))
        self.cap = float(cfg.get("max_usd_per_token", 2000))
        self.min_usd = float(cfg.get("min_usd", 50))
        self.delay = float(cfg.get("entry_delay_s", 15))
        self.max_age = float(cfg.get("max_signal_age_s", 120))
        self.max_open = int(cfg.get("max_open_positions", 10))
        self.stop = float(cfg.get("safety_stop", 0.60))
        self.mark_s = float(cfg.get("mark_interval_s", 60))
        self.logs_s = float(cfg.get("logs_poll_s", 15))
        self.dex_confirm_s = float(cfg.get("dex_confirm_s", 15))
        self.max_span = int(cfg.get("max_log_span", 5000))
        self.sell_retries = int(cfg.get("sell_retries", 3))
        self.kill = kill_switch_file
        self.positions: dict[tuple[str, str], Position] = {
            (p.chain, p.token): p for p in store.load_open(True, self.chains) if p.timing.get("xchain")}
        self.locks: dict[tuple[str, str], asyncio.Lock] = defaultdict(asyncio.Lock)
        self._opening: set[tuple[str, str]] = set()
        self.client = self.session = None
        self.rpc: dict[str, str] = {}
        self._tasks: set[asyncio.Task] = set()
        self._last_block: dict[str, int] = {}
        self._seen_logs: OrderedDict[str, None] = OrderedDict()
        self._logs_warned: set[str] = set()

    # ---------- inputs ----------
    def on_his_trade(self, t: XTrade, leader: str, evm: str, detected_at: float):
        """Called by the watcher for every NEW Relay request it records. Cheap; never raises."""
        if t.chain not in self.chains or t.status != "success" or leader not in self.leaders:
            return
        if t.side == "buy":
            self._spawn(self._buy(t, leader, evm, detected_at))
        elif t.side == "sell":
            his = {"his_ref": t.request_id, "his_ts": t.deposit_ts, "his_usd": t.usd, "his_price_usd": t.price_usd}
            self._spawn(self._mirror_sell(t.chain, t.token, evm, t.token_amount, detected_at, "leader_sold", his))

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
        key = (t.chain, t.token)
        async with self.locks[key]:
            p = self.positions.get(key)
            if p is not None:                                    # his holding, for the no-RPC fallback
                p.timing["his_bought"] = str(int(p.timing.get("his_bought", "0")) + t.token_amount)
            age = detected_at - (t.deposit_ts or detected_at)
            target = self.mult * t.usd
            size = min(target, self.cap - (p.timing["bought_usd"] if p else 0.0))
            why = None
            if age > self.max_age:
                why = f"stale: detected {age:.0f}s after his trade"
            elif os.path.exists(self.kill):
                why = "kill switch file present"
            elif p is None and len(self.positions) + len(self._opening) >= self.max_open:
                why = f"max open xchain positions ({self.max_open})"
            elif size < self.min_usd:
                why = (f"cap ${self.cap:,.0f}/token reached" if target >= self.min_usd
                       else f"our size ${target:,.2f} < ${self.min_usd:,.0f}")
            if why:
                self._skip(t, leader, why)
                if p is not None:
                    self.store.update_position(p)
                return
            if p is None:
                self._opening.add(key)
            try:
                await self._sleep_until(detected_at + self.delay)
                q = await self._quote("buy", leader, evm, t.chain_id, t.token, int(size * 1e6))
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
                    p.id = self.store.open_position(p)
                    self.positions[key] = p
                    kind = "open"
                else:
                    p.token_amount += q.out_raw
                    p.cost += cost
                    p.timing["remaining"] = str(int(p.timing["remaining"]) + q.out_raw)
                    p.timing["basis_usd"] += cost
                    p.timing["bought_usd"] += size
                    self.store.update_position(p)
                    kind = "add"
            finally:
                self._opening.discard(key)
            our_price = cost / (q.out_raw / 10 ** t.decimals)
            self._fill(p, "buy", kind, 1.0, now, q, cost, q.out_raw, our_price,
                       {"his_ref": t.request_id, "his_ts": t.deposit_ts, "his_usd": t.usd,
                        "his_price_usd": t.price_usd})

    # ---------- exits ----------
    async def _mirror_sell(self, chain: str, token: str, evm: str, sold_raw: int, detected_at: float,
                           reason: str, his: dict):
        key = (chain, token)
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
            q = await self._quote("sell", p.leader, evm, int(p.timing["chain_id"]), p.token, amount)
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
            self.positions.pop((p.chain, p.token), None)
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

    # ---------- marks + safety stop ----------
    async def mark_once(self):
        for p in list(self.positions.values()):
            if self.locks[(p.chain, p.token)].locked():
                continue                                         # a buy/sell is in progress
            rem = int(p.timing["remaining"])
            q = await self._quote("sell", p.leader, p.timing["evm"], int(p.timing["chain_id"]), p.token, rem)
            if q is None:
                continue
            value_rem = q.out_ui - q.gas_usd
            self.store.mark(p, p.timing["realized_usd"] + value_rem)
            if value_rem <= (1 - self.stop) * p.timing["basis_usd"]:
                log.warning("xchain SAFETY STOP %s %s: remaining worth $%.2f vs cost $%.2f (%+.0f%%)",
                            p.chain, p.timing.get("symbol"), value_rem, p.timing["basis_usd"],
                            100 * (value_rem / p.timing["basis_usd"] - 1))
                self._spawn(self._stop(p, time.time()))

    async def _stop(self, p: Position, triggered_at: float):
        async with self.locks[(p.chain, p.token)]:
            if (p.chain, p.token) not in self.positions:
                return
            await self._sleep_until(triggered_at + self.delay)
            await self._sell(p, int(p.timing["remaining"]), "safety_stop", 1.0, {})

    # ---------- DEX sells (ERC-20 Transfer logs out of his EVM address) ----------
    async def logs_once(self):
        for chain in self.chains:
            held = {p.token.lower() for p in self.positions.values() if p.chain == chain}
            url = self.rpc.get(chain)
            if not held:
                self._last_block.pop(chain, None)
                continue
            if not url:
                self._warn_logs(chain, "no RPC configured")
                continue
            latest = await self._eth(url, "eth_blockNumber", [])
            if not isinstance(latest, str):
                continue
            latest = int(latest, 16)
            frm = max(self._last_block.get(chain, latest - self.max_span) + 1, latest - self.max_span)
            if frm > latest:
                continue
            evms = sorted({p.timing["evm"] for p in self.positions.values() if p.chain == chain})
            logs = await self._eth(url, "eth_getLogs", [{"fromBlock": hex(frm), "toBlock": hex(latest),
                                                         "address": sorted(held),
                                                         "topics": [TRANSFER, [pad_addr(e) for e in evms]]}])
            if not isinstance(logs, list):
                self._warn_logs(chain, f"eth_getLogs failed: {str(logs)[:150]}")
                continue
            self._last_block[chain] = latest
            for lg in logs:
                k = f"{lg['transactionHash']}:{lg.get('logIndex')}"
                if k in self._seen_logs:
                    continue
                self._seen_logs[k] = None
                if len(self._seen_logs) > 5000:
                    self._seen_logs.popitem(last=False)
                self._spawn(self._dex_check(chain, url, lg, time.time()))

    def _warn_logs(self, chain: str, why: str):
        if chain not in self._logs_warned:
            self._logs_warned.add(chain)
            log.warning("xchain: DEX sells on %s are NOT detected (%s) - only Relay sells are mirrored", chain, why)
            self.store.record_event(time.time(), "", "", "xchain_logs_unavailable", "", None, f"{chain}: {why}")

    async def _dex_check(self, chain: str, url: str, lg: dict, detected_at: float):
        """A token left his EVM wallet. Relay request with that deposit tx -> the Relay path handles it;
        otherwise he sold on a DEX (or moved it away): mirror it."""
        tx, token = lg["transactionHash"], lg["address"].lower()
        evm = "0x" + lg["topics"][1][-40:]
        amount = int(lg["data"], 16) if lg.get("data") not in (None, "0x") else 0
        await asyncio.sleep(self.dex_confirm_s)
        if self.store.xtrade_has_deposit(tx) or await self.client.requests(depositTxHash=tx):
            return
        leader = next((s for s, e in self.leaders.items() if e == evm), "")
        p = self.positions.get((chain, token)) or next(
            (q for (c, t), q in self.positions.items() if c == chain and t.lower() == token), None)
        if p is None or amount <= 0:
            return
        blk = await self._eth(url, "eth_getBlockByNumber", [lg["blockNumber"], False])
        his_ts = int(blk["timestamp"], 16) if isinstance(blk, dict) and blk.get("timestamp") else None
        log.info("xchain DEX sell by %s on %s: %s raw %s (tx %s)", evm[:10], chain, amount,
                 p.timing.get("symbol"), tx[:12])
        self.store.record_event(time.time(), leader, tx, "xchain_dex_sell", p.token, None,
                                f"{chain}: {amount / 10 ** int(p.timing['decimals']):,.6g} "
                                f"{p.timing.get('symbol')} out of {evm[:10]} without a Relay request")
        await self._mirror_sell(chain, p.token, evm, amount, detected_at, "leader_sold_dex",
                                {"his_ref": tx, "his_ts": his_ts, "his_usd": None, "his_price_usd": None})

    # ---------- helpers ----------
    def _fill(self, p: Position, side: str, reason: str, frac: float, now: float, q: Quote, usd: float,
              tokens: int, our_price: float, his: dict):
        hp = his.get("his_price_usd")
        gap = (our_price / hp - 1) * 100 if hp and our_price else None
        delay = now - his["his_ts"] if his.get("his_ts") else None
        self.store.record_xfill(ts=now, position_id=p.id, chain=p.chain, token=p.token,
                                symbol=p.timing.get("symbol"), side=side, reason=reason, fraction=frac,
                                our_usd=usd, our_tokens=tokens, our_price_usd=our_price, gap_pct=gap,
                                delay_s=delay, impact_pct=q.impact_pct, gas_usd=q.gas_usd, **his)
        log.info("xchain paper %s %s %s: $%.2f for %s %s | ours $%.4g vs his %s (%s) | delay %s | impact %s%%",
                 side.upper(), reason, p.chain, usd, f"{tokens / 10 ** int(p.timing['decimals']):,.6g}",
                 p.timing.get("symbol"), our_price, f"${hp:.4g}" if hp else "n/a (DEX)",
                 f"{gap:+.1f}%" if gap is not None else "-", f"{delay:.1f}s" if delay is not None else "-",
                 q.impact_pct)

    def _skip(self, t: XTrade, leader: str, why: str):
        log.info("xchain paper skip %s %s $%.2f: %s", t.chain, t.symbol, t.usd, why)
        self.store.record_event(time.time(), leader, t.deposit_tx, "xchain_paper_skip", t.token, None,
                                f"{t.chain} {t.symbol} his ${t.usd:,.2f}: {why}")

    async def _quote(self, side, sol_user, evm, chain_id, token, amount_raw) -> Quote | None:
        if amount_raw <= 0 or self.client is None:
            return None
        return parse_quote(await self.client.price(price_body(side, sol_user, evm, chain_id, token, amount_raw)) or {})

    async def _balance(self, chain: str, token: str, evm: str) -> int | None:
        url = self.rpc.get(chain)
        if not url:
            return None
        r = await self._eth(url, "eth_call", [{"to": token, "data": "0x70a08231" + pad_addr(evm)[2:]}, "latest"])
        try:
            return int(r, 16)
        except (TypeError, ValueError):
            return None

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

    async def run(self, client, session, rpc: dict):
        self.client, self.session, self.rpc = client, session, dict(rpc)
        log.info("xchain PAPER trading on %s: %.2fx his size, cap $%.0f/token, min $%.0f, delay %.0fs, "
                 "stop -%.0f%%, max %d open (%d resumed); DEX-sell logs on %s", sorted(self.chains), self.mult,
                 self.cap, self.min_usd, self.delay, self.stop * 100, self.max_open, len(self.positions),
                 [c for c in sorted(self.chains) if self.rpc.get(c)] or "none")
        next_mark = time.time() + min(self.mark_s, 10)
        while True:
            try:
                await self.logs_once()
                if time.time() >= next_mark:
                    next_mark = time.time() + self.mark_s
                    await self.mark_once()
            except Exception as e:
                log.warning("xchain paper loop error: %s", e)
            await asyncio.sleep(self.logs_s)
