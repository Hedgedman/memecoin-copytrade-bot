"""Solana adapter.

Detection: one WebSocket, one logsSubscribe per leader wallet, then getTransaction
and token-balance deltas. Balance deltas are DEX-agnostic: it works the same for
pump.fun curves, Raydium, Meteora, or DFlow-routed fills, because we only look at
what the wallet gained and lost, not at which program did it.

Pricing: for Pump.fun curve and PumpSwap tokens the pool accounts are streamed over a
websocket (bot/pricing/pools.py). Pre-trade checks, exit marks, stops and paper fills
are computed locally from pool state: no Jupiter call, no rate limit, exact price impact.
Other venues fall back to Jupiter quotes.

Execution (live): Jupiter quote + swap API, with priority over background quotes.
Upgrade path once an edge is proven: gRPC detection, direct instruction building, Jito.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import time
from collections import OrderedDict
from dataclasses import dataclass, field

import aiohttp
import websockets
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction

from ..models import Fill, Position, Signal
from ..pricing.pools import (AccountHub, PoolState, PUMP_PROGRAM, PUMPSWAP_PROGRAM,
                             curve_pda, decode_curve, decode_token_amount, find_pool_vaults)
from .base import ChainAdapter, RateLimited

log = logging.getLogger("solana")

WSOL = "So11111111111111111111111111111111111111112"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
LAMPORTS = 1_000_000_000


def parse_wallet_swap(tx: dict | None, wallet: str, min_quote_sol: float = 0.001):
    """Classify a jsonParsed transaction from `wallet`'s point of view.

    Returns (side, mint, token_amount_raw, sol_amount) or None.
    Only SOL/WSOL-quoted single-token swaps are recognised; USDC/USDT-quoted
    trades, multi-token transactions and plain transfers return None.
    """
    if not tx or not tx.get("meta") or tx["meta"].get("err") is not None:
        return None
    meta = tx["meta"]
    keys = [k["pubkey"] if isinstance(k, dict) else k
            for k in tx["transaction"]["message"]["accountKeys"]]

    lamport_delta = 0
    if wallet in keys:
        i = keys.index(wallet)
        lamport_delta = meta["postBalances"][i] - meta["preBalances"][i]

    deltas: dict[str, int] = {}
    for balances, sign in ((meta.get("preTokenBalances") or [], -1),
                           (meta.get("postTokenBalances") or [], 1)):
        for b in balances:
            if b.get("owner") != wallet:
                continue
            deltas[b["mint"]] = deltas.get(b["mint"], 0) + sign * int(b["uiTokenAmount"]["amount"])

    lamport_delta += deltas.pop(WSOL, 0)       # WSOL is 1:1 lamports
    stable_moved = any(deltas.pop(m, 0) for m in (USDC, USDT))
    deltas = {m: d for m, d in deltas.items() if d != 0}
    if len(deltas) != 1 or stable_moved:
        return None

    mint, d = next(iter(deltas.items()))
    sol = lamport_delta / LAMPORTS
    if d > 0 and sol < -min_quote_sol:
        return ("buy", mint, d, -sol)
    if d < 0 and sol > min_quote_sol:
        return ("sell", mint, -d, sol)
    return None

# Known Solana trading programs, used only for LABELLING trades (not for trading).
# Anything not listed is logged as "?" + the first characters of its program id.
VENUE_PROGRAMS = {
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P": "pumpfun_curve",
    "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA": "pumpswap_amm",
    "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8": "raydium_amm_v4",
    "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C": "raydium_cpmm",
    "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK": "raydium_clmm",
    "LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj": "raydium_launchlab",
    "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo": "meteora_dlmm",
    "Eo7WjKq67rjJQSZxS6z3YkapzY3eMj6Xy8X5EQVn5UaB": "meteora_damm_v1",
    "cpamdpZCGKUy5JxQXB4dcpGPiikHawvSWAd6mEn1sGG": "meteora_damm_v2",
    "dbcij3LWUppWqq96dh6gJWwBifmcGfLSB5D4DuSMaqN": "meteora_dbc",
    "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc": "orca_whirlpool",
    "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4": "jupiter",
}
# Infrastructure programs that appear in almost every transaction: never interesting.
INFRA_PROGRAMS = {
    "11111111111111111111111111111111", "ComputeBudget111111111111111111111111111111",
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA", "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",
    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL", "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr",
    "Memo1UhkJRfHyvLMcVucJwxXeuD728EqVDDwQDxFMNo",
}


def venues_in_tx(tx: dict | None) -> str:
    """Comma-separated list of trading programs a transaction went through."""
    if not tx:
        return ""
    ids = set()
    for ix in tx.get("transaction", {}).get("message", {}).get("instructions", []) or []:
        ids.add(ix.get("programId"))
    for inner in (tx.get("meta") or {}).get("innerInstructions") or []:
        for ix in inner.get("instructions", []) or []:
            ids.add(ix.get("programId"))
    names = set()
    for pid in ids:
        if not pid or pid in INFRA_PROGRAMS:
            continue
        names.add(VENUE_PROGRAMS.get(pid, "?" + pid[:6]))
    return ",".join(sorted(names))


def parse_stable_swap(tx: dict | None, wallet: str, min_usd: float = 1.0):
    """Like parse_wallet_swap, but for trades paid/received in USDC or USDT.

    Returns (side, mint, token_amount_raw, usd_amount) or None.
    """
    if not tx or not tx.get("meta") or tx["meta"].get("err") is not None:
        return None
    meta = tx["meta"]
    deltas: dict[str, int] = {}
    for balances, sign in ((meta.get("preTokenBalances") or [], -1),
                           (meta.get("postTokenBalances") or [], 1)):
        for b in balances:
            if b.get("owner") != wallet:
                continue
            deltas[b["mint"]] = deltas.get(b["mint"], 0) + sign * int(b["uiTokenAmount"]["amount"])
    usd = (deltas.pop(USDC, 0) + deltas.pop(USDT, 0)) / 1e6   # both have 6 decimals
    deltas.pop(WSOL, None)
    deltas = {m: d for m, d in deltas.items() if d != 0}
    if len(deltas) != 1:
        return None
    mint, d = next(iter(deltas.items()))
    if d > 0 and usd < -min_usd:
        return ("buy", mint, d, -usd)
    if d < 0 and usd > min_usd:
        return ("sell", mint, -d, usd)
    return None


class _Throttle:
    """Spaces out Jupiter calls and backs off after a rate-limit response.

    Two lanes: priority callers (entry checks, buys, sells) reserve the next free slot
    immediately; background callers (exit quotes, SOL price) only take a slot that is
    free right now and nobody with priority is waiting. So a new signal never queues
    behind 30 exit quotes.
    """

    def __init__(self, max_rps: float):
        self.interval = 1.0 / max_rps if max_rps > 0 else 0.0
        self.next_ok = 0.0
        self.cooldown_until = 0.0
        self.backoff = 2.0
        self.hi_waiting = 0

    async def wait(self, priority: bool = False):
        if priority:
            self.hi_waiting += 1
            try:
                t = max(self.next_ok, self.cooldown_until, time.time())
                self.next_ok = t + self.interval          # reserved; no await in between
                await asyncio.sleep(max(0.0, t - time.time()))
            finally:
                self.hi_waiting -= 1
            return
        while True:
            now = time.time()
            t = max(self.next_ok, self.cooldown_until)
            if not self.hi_waiting and t <= now:
                self.next_ok = now + self.interval
                return
            await asyncio.sleep(min(0.1, max(0.01, t - now)))

    def hit_limit(self) -> float:
        pause = self.backoff
        self.cooldown_until = time.time() + pause
        self.backoff = min(self.backoff * 2, 30.0)
        return pause

    def ok(self):
        self.backoff = 2.0


@dataclass
class _Pool:
    venue: str
    accounts: list[str]                       # what we subscribe to
    token_vault: str | None = None            # pumpswap only
    sol_vault: str | None = None
    amounts: dict = field(default_factory=dict)   # vault -> (amount, slot)
    state: PoolState | None = None
    pending: asyncio.TimerHandle | None = None


class SolanaAdapter(ChainAdapter):
    name = "solana"

    def __init__(self, cfg: dict, paper: bool):
        self.cfg = cfg
        self.paper = paper
        self.rpc = cfg["rpc_http"]
        self.ws_url = cfg["rpc_ws"]
        self.leaders: list[str] = cfg["leaders"]
        self.jup = cfg.get("jupiter_base", "https://lite-api.jup.ag/swap/v1").rstrip("/")
        self.jup_key = os.environ.get(cfg.get("jupiter_api_key_env", "JUPITER_API_KEY"), "")
        self.slippage_bps = int(cfg.get("slippage_bps", 1500))
        self.max_prio = int(cfg.get("max_priority_fee_lamports", 2_000_000))
        self.min_leader_buy = float(cfg.get("min_leader_buy_sol", 0.5))
        self.max_roundtrip_loss = float(cfg.get("max_roundtrip_loss", 0.08))
        self.reject_freeze = bool(cfg.get("reject_freeze_authority", True))
        self.reject_mint_auth = bool(cfg.get("reject_mint_authority", True))
        self.confirm_timeout = float(cfg.get("confirm_timeout_s", 60))
        self.max_mc_usd = float(cfg.get("max_market_cap_usd", 0))      # 0 = no filter
        self.paper_fee = float(cfg.get("paper_fee_per_tx_sol", 0.0))   # simulated fee per tx in paper mode
        # --- pool pricing ---
        self.pool_venues = set(cfg.get("pool_pricing_venues", ["pumpfun_curve", "pumpswap_amm"]))
        self.fee_bps = {"pumpfun_curve": int(cfg.get("pumpfun_fee_bps", 125)),
                        "pumpswap_amm": int(cfg.get("pumpswap_fee_bps", 30))}
        self.paper_delay = float(cfg.get("paper_land_delay_s", 1.5))   # decision -> "landed" in paper mode
        self.tick_tail = float(cfg.get("tick_tail_s", 900))            # keep recording after close/skip
        self.max_watched = int(cfg.get("max_watched_pools", 60))
        self.record_skipped = bool(cfg.get("record_skipped", True))
        self.hub = AccountHub(self.ws_url, self._on_account, on_connect=self._refresh_all)
        self._pools: dict[str, _Pool] = {}
        self._acct_token: dict[str, str] = {}
        self._hints: OrderedDict[str, tuple[str, str, str]] = OrderedDict()   # mint -> pumpswap vaults
        self._graduated: set[str] = set()
        self._pinned: set[str] = set()
        self._expires: dict[str, float] = {}
        self._price_cb = None
        self._tick_sink = None
        self._tick_buf: list[tuple] = []
        self._stop = asyncio.Event()
        self._tasks: list[asyncio.Task] = []
        # ---
        self._sol_usd_val, self._sol_usd_ts = 0.0, 0.0
        self.throttle = _Throttle(float(cfg.get("jupiter_max_rps", 1.0)))
        self.kp: Keypair | None = None
        key = os.environ.get(cfg.get("private_key_env", "SOL_PRIVATE_KEY"))
        if key:
            self.kp = Keypair.from_base58_string(key)
        elif not paper:
            raise RuntimeError("Live mode needs a private key in the env var named by private_key_env")
        self.session: aiohttp.ClientSession | None = None
        self._seen: OrderedDict[str, None] = OrderedDict()

    # ---------- plumbing ----------
    async def start(self):
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))
        if self.kp:
            log.info("trading wallet %s (paper=%s)", self.kp.pubkey(), self.paper)
        self._tasks = [asyncio.create_task(self.hub.run(self._stop)),
                       asyncio.create_task(self._flush_ticks_loop()),
                       asyncio.create_task(self._janitor_loop()),
                       asyncio.create_task(self._sol_usd_loop())]
        log.info("pool pricing for %s (fees bps %s), paper fill delay %.2fs",
                 sorted(self.pool_venues), self.fee_bps, self.paper_delay)

    async def close(self):
        self._stop.set()
        self._flush_ticks()
        for t in self._tasks:
            t.cancel()
        if self.session:
            await self.session.close()

    async def _rpc(self, method: str, params: list):
        async with self.session.post(self.rpc, json={"jsonrpc": "2.0", "id": 1,
                                                     "method": method, "params": params}) as r:
            j = await r.json(content_type=None)
        if "error" in j:
            raise RuntimeError(f"{method}: {j['error']}")
        return j["result"]

    async def _get_tx(self, sig: str, tries: int = 8):
        for i in range(tries):
            try:
                tx = await self._rpc("getTransaction", [sig, {
                    "encoding": "jsonParsed", "maxSupportedTransactionVersion": 0,
                    "commitment": "confirmed"}])
                if tx:
                    return tx
            except Exception as e:  # node lag right after the notification is normal
                log.debug("getTransaction %s attempt %d: %s", sig, i, e)
            await asyncio.sleep(0.2 * (i + 1))
        return None

    def _mark_seen(self, sig: str) -> bool:
        if sig in self._seen:
            return True
        self._seen[sig] = None
        if len(self._seen) > 20_000:
            self._seen.popitem(last=False)
        return False

    # ---------- push-based pricing hooks (see ChainAdapter) ----------
    def set_price_callback(self, cb):
        self._price_cb = cb

    def set_tick_sink(self, sink):
        self._tick_sink = sink

    async def track(self, token: str) -> None:
        self._pinned.add(token)
        if token not in self._pools and token not in self._graduated:
            try:                                  # e.g. position resumed after a restart
                _, pool = await self._resolve(token)
                if pool:
                    await self._register(token, pool)
            except Exception as e:
                log.debug("track %s: %s", token, e)

    async def release(self, token: str) -> None:
        self._pinned.discard(token)
        self._expires[token] = time.time() + self.tick_tail

    async def observe(self, token: str) -> None:
        if not self.record_skipped or token in self._pools or token in self._graduated:
            return
        try:
            _, pool = await self._resolve(token)
            if pool:
                await self._register(token, pool)
        except Exception as e:
            log.debug("observe %s: %s", token, e)

    # ---------- pool state ----------
    async def _resolve(self, token: str) -> tuple[dict, _Pool | None]:
        """ONE getMultipleAccounts: mint info (authorities, supply) + curve + pumpswap vaults."""
        keys = [token]
        want_curve = "pumpfun_curve" in self.pool_venues and token not in self._graduated
        if want_curve:
            cpda = curve_pda(token)
            keys.append(cpda)
        hint = self._hints.get(token) if "pumpswap_amm" in self.pool_venues else None
        if hint:
            keys += list(hint)
        res = await self._rpc("getMultipleAccounts",
                              [keys, {"encoding": "jsonParsed", "commitment": "processed"}])
        slot, vals, now = res["context"]["slot"], res["value"], time.time()
        mdata = (vals[0] or {}).get("data")
        mint_info = mdata.get("parsed", {}).get("info", {}) if isinstance(mdata, dict) else {}
        i, pool = 1, None
        if want_curve:
            v = vals[i]; i += 1
            if v and v.get("owner") == PUMP_PROGRAM and isinstance(v.get("data"), list):
                try:
                    st = decode_curve(base64.b64decode(v["data"][0]), slot, now)
                    if st.complete:
                        self._graduated.add(token)
                    else:
                        pool = _Pool("pumpfun_curve", [cpda], state=st)
                except ValueError:
                    pass
        if pool is None and hint:
            owner_acc, tv_acc, sv_acc = vals[i:i + 3]
            if owner_acc and owner_acc.get("owner") == PUMPSWAP_PROGRAM and tv_acc and sv_acc:
                ta, sa = _amount_of(tv_acc), _amount_of(sv_acc)
                if ta and sa:
                    pool = _Pool("pumpswap_amm", [hint[1], hint[2]], token_vault=hint[1],
                                 sol_vault=hint[2], amounts={hint[1]: (ta, slot), hint[2]: (sa, slot)},
                                 state=PoolState("pumpswap_amm", ta, sa, slot=slot, recv_ts=now))
        return mint_info, pool

    async def _register(self, token: str, pool: _Pool) -> None:
        if token in self._pools:
            return
        self._pools[token] = pool
        for a in pool.accounts:
            self._acct_token[a] = token
            await self.hub.watch(a)
        if token not in self._pinned:
            self._expires[token] = time.time() + self.tick_tail
        if pool.state:
            self._emit(token, pool.state)

    def _drop(self, token: str) -> None:
        pool = self._pools.pop(token, None)
        self._expires.pop(token, None)
        if not pool:
            return
        if pool.pending:
            pool.pending.cancel()
        for a in pool.accounts:
            self._acct_token.pop(a, None)
            asyncio.ensure_future(self.hub.unwatch(a))

    def _on_account(self, key: str, data: bytes | None, slot: int, recv_ts: float) -> None:
        token = self._acct_token.get(key)
        pool = self._pools.get(token) if token else None
        if pool is None or data is None:
            return
        try:
            if pool.venue == "pumpfun_curve":
                self._set_state(token, pool, decode_curve(data, slot, recv_ts))
                return
            pool.amounts[key] = (decode_token_amount(data), slot)
        except ValueError as e:
            log.debug("bad account data for %s: %s", key, e)
            return
        # PumpSwap: a trade changes both vaults in the same slot, but they arrive as two
        # messages. Combine when both are at the same slot; otherwise wait 50 ms so we
        # never price a half-updated pool (that would fake a pump or a dump).
        tv, sv = pool.amounts.get(pool.token_vault), pool.amounts.get(pool.sol_vault)
        if not tv or not sv:
            return
        if tv[1] == sv[1]:
            if pool.pending:
                pool.pending.cancel(); pool.pending = None
            self._set_state(token, pool, PoolState("pumpswap_amm", tv[0], sv[0], slot=tv[1], recv_ts=recv_ts))
        elif pool.pending is None:
            pool.pending = asyncio.get_running_loop().call_later(0.05, self._flush_pair, token)

    def _flush_pair(self, token: str) -> None:
        pool = self._pools.get(token)
        if not pool:
            return
        pool.pending = None
        tv, sv = pool.amounts.get(pool.token_vault), pool.amounts.get(pool.sol_vault)
        if tv and sv and tv[0] and sv[0]:
            self._set_state(token, pool, PoolState("pumpswap_amm", tv[0], sv[0],
                                                   slot=max(tv[1], sv[1]), recv_ts=time.time()))

    def _set_state(self, token: str, pool: _Pool, st: PoolState) -> None:
        if st.venue == "pumpfun_curve" and st.complete:
            log.info("%s graduated off the bonding curve -> Jupiter pricing from now on", token)
            self._graduated.add(token)
            self._drop(token)
            return
        pool.state = st
        self._emit(token, st)
        if self._price_cb:
            self._price_cb(token)

    def _emit(self, token: str, st: PoolState) -> None:
        self._tick_buf.append((st.recv_ts, st.slot, token, st.venue, st.tok_res, st.sol_res,
                               st.real_tok, st.real_sol))

    async def _refresh(self, tokens: list[str]) -> None:
        """Re-read pool accounts over HTTP (after a reconnect, or if the hub is down)."""
        keys = [a for t in tokens if t in self._pools for a in self._pools[t].accounts]
        for i in range(0, len(keys), 100):
            chunk = keys[i:i + 100]
            res = await self._rpc("getMultipleAccounts",
                                  [chunk, {"encoding": "base64", "commitment": "processed"}])
            slot, now = res["context"]["slot"], time.time()
            for key, v in zip(chunk, res["value"]):
                if v:
                    self._on_account(key, base64.b64decode(v["data"][0]), slot, now)

    async def _refresh_all(self) -> None:
        try:
            await self._refresh(list(self._pools))
        except Exception as e:
            log.warning("pool refresh after reconnect failed: %s", e)

    def _pool_value(self, pos: Position) -> float | None:
        """Exit value from local pool state, or None if this token isn't pool-priced."""
        pool = self._pools.get(pos.token)
        if not pool or not pool.state:
            return None
        st, fee = pool.state, self.fee_bps[pool.venue]
        if self.paper:
            # shadow pool: in reality our own buy would be in the pool (see PoolState.after_buy)
            st = st.after_buy(int((pos.cost - self.paper_fee) * LAMPORTS), fee)
        return st.sell_out(pos.token_amount, fee) / LAMPORTS

    # ---------- background loops ----------
    def _flush_ticks(self):
        if self._tick_buf and self._tick_sink:
            rows, self._tick_buf = self._tick_buf, []
            try:
                self._tick_sink(rows)
            except Exception as e:
                log.error("writing ticks failed: %s", e)
        elif not self._tick_sink:
            self._tick_buf.clear()

    async def _flush_ticks_loop(self):
        while True:
            await asyncio.sleep(1.0)
            self._flush_ticks()

    async def _janitor_loop(self):
        while True:
            await asyncio.sleep(15)
            now = time.time()
            for t in [t for t in self._pools if t not in self._pinned and self._expires.get(t, 0) < now]:
                self._drop(t)
            extra = len(self._pools) - self.max_watched
            if extra > 0:   # over budget: drop unpinned pools closest to expiring
                for t in sorted((t for t in self._pools if t not in self._pinned),
                                key=lambda t: self._expires.get(t, 0))[:extra]:
                    self._drop(t)

    async def _sol_usd_loop(self):
        while True:
            try:
                await self._sol_usd(refresh=True)
            except Exception as e:
                log.debug("SOL price refresh failed: %s", e)
            await asyncio.sleep(60)

    # ---------- detection ----------
    async def listen(self, queue: asyncio.Queue):
        backoff = 1
        while True:
            try:
                async with websockets.connect(self.ws_url, ping_interval=20,
                                              max_size=2 ** 22) as ws:
                    pending, subs = {}, {}
                    for i, w in enumerate(self.leaders, start=1):
                        await ws.send(json.dumps({
                            "jsonrpc": "2.0", "id": i, "method": "logsSubscribe",
                            "params": [{"mentions": [w]}, {"commitment": "confirmed"}]}))
                        pending[i] = w
                    backoff = 1
                    log.info("subscribing to %d leader wallets", len(self.leaders))
                    async for raw in ws:
                        notified = time.time()
                        m = json.loads(raw)
                        if m.get("id") in pending:
                            w = pending.pop(m["id"])
                            if "result" in m:
                                subs[m["result"]] = w
                            else:
                                log.error("subscribe failed for %s: %s", w, m.get("error"))
                            continue
                        if m.get("method") != "logsNotification":
                            continue
                        p = m["params"]
                        leader = subs.get(p["subscription"])
                        v = p["result"]["value"]
                        if not leader or v.get("err") is not None or self._mark_seen(v["signature"]):
                            continue
                        asyncio.create_task(self._handle(v["signature"], leader, queue, notified))
            except Exception as e:
                log.warning("websocket dropped (%s); reconnecting in %ss", e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)

    async def _handle(self, sig: str, leader: str, queue: asyncio.Queue, notified: float | None = None):
        tx = await self._get_tx(sig)
        fetched = time.time()
        keys = (tx or {}).get("transaction", {}).get("message", {}).get("accountKeys", [])
        if not any(isinstance(k, dict) and k.get("pubkey") == leader and k.get("signer") for k in keys):
            return   # leader didn't sign: airdrop, fee payout, or account created by someone else
        parsed = parse_wallet_swap(tx, leader)
        if parsed:
            side, mint, amount, sol = parsed
        else:
            stable = parse_stable_swap(tx, leader)
            if not stable:
                log.info("leader %s tx not recognised as swap: %s", leader[:6], sig)
                return
            side, mint, amount, usd = stable
            try:
                sol = usd / await self._sol_usd()  # convert to SOL so min_leader_buy_sol still applies
            except Exception as e:
                log.warning("SOL price unavailable, skipping USDC trade %s: %s", sig, e)
                return
        if side == "buy" and sol < self.min_leader_buy:
            log.info("ignore small leader buy %.3f SOL %s", sol, mint)
            return
        s = Signal(self.name, leader, side, mint, amount, sol, sig, tx.get("blockTime"))
        s.venues = venues_in_tx(tx)
        s.notified_at, s.fetched_at, s.slot = notified, fetched, tx.get("slot")
        if side == "buy" and "pumpswap_amm" in s.venues:
            vaults = find_pool_vaults(tx, mint, leader)
            if vaults:
                self._hints[mint] = vaults
                if len(self._hints) > 5000:
                    self._hints.popitem(last=False)
        await queue.put(s)

    # ---------- Jupiter ----------
    def _headers(self):
        return {"x-api-key": self.jup_key} if self.jup_key else {}

    async def _quote(self, in_mint: str, out_mint: str, amount: int, slippage_bps: int | None = None,
                     priority: bool = False):
        params = {"inputMint": in_mint, "outputMint": out_mint, "amount": str(amount),
                  "slippageBps": str(slippage_bps or self.slippage_bps)}
        await self.throttle.wait(priority)
        async with self.session.get(f"{self.jup}/quote", params=params, headers=self._headers()) as r:
            text = await r.text()
        if r.status == 429 or "rate limit" in text.lower():
            pause = self.throttle.hit_limit()
            log.warning("Jupiter rate limit hit - pausing quotes for %.0fs", pause)
            raise RateLimited("jupiter rate limit")
        self.throttle.ok()
        try:
            q = json.loads(text)
        except ValueError:
            raise RuntimeError(f"quote failed: {text[:200]}")
        if r.status != 200 or "outAmount" not in q:
            raise RuntimeError(f"quote failed: {q}")
        return q

    async def _swap(self, quote: dict, mint: str) -> Fill:
        body = {"quoteResponse": quote, "userPublicKey": str(self.kp.pubkey()),
                "wrapAndUnwrapSol": True, "dynamicComputeUnitLimit": True,
                "prioritizationFeeLamports": {"priorityLevelWithMaxLamports": {
                    "maxLamports": self.max_prio, "priorityLevel": "veryHigh"}}}
        await self.throttle.wait(priority=True)
        async with self.session.post(f"{self.jup}/swap", json=body, headers=self._headers()) as r:
            sw = await r.json(content_type=None)
        if "swapTransaction" not in sw:
            return Fill(False, None, 0, 0.0, f"swap build failed: {sw}")
        unsigned = VersionedTransaction.from_bytes(base64.b64decode(sw["swapTransaction"]))
        signed = VersionedTransaction(unsigned.message, [self.kp])
        sig = await self._rpc("sendTransaction", [base64.b64encode(bytes(signed)).decode(),
                                                  {"encoding": "base64", "skipPreflight": True,
                                                   "maxRetries": 3}])
        status = await self._confirm(sig)
        if status != "ok":
            return Fill(False, sig, 0, 0.0, status)
        tx = await self._get_tx(sig)
        parsed = parse_wallet_swap(tx, str(self.kp.pubkey()), min_quote_sol=0.0)
        if not parsed or parsed[1] != mint:
            return Fill(False, sig, 0, 0.0, "confirmed but fill could not be parsed - CHECK WALLET")
        return Fill(True, sig, parsed[2], parsed[3], slot=(tx or {}).get("slot"), model="live")

    async def _confirm(self, sig: str) -> str:
        deadline = time.time() + self.confirm_timeout
        while time.time() < deadline:
            res = await self._rpc("getSignatureStatuses", [[sig], {"searchTransactionHistory": False}])
            st = res["value"][0]
            if st:
                if st.get("err"):
                    return f"failed on-chain: {st['err']}"
                if st.get("confirmationStatus") in ("confirmed", "finalized"):
                    return "ok"
            await asyncio.sleep(0.4)
        return "not confirmed in time - may still land, CHECK WALLET"

    async def _sol_usd(self, refresh: bool = False) -> float:
        """SOL price in USD from a Jupiter SOL->USDC quote. Refreshed in the background
        every 60 s, so the entry path normally never waits for it."""
        if refresh or not self._sol_usd_val or time.time() - self._sol_usd_ts > 300:
            q = await self._quote(WSOL, USDC, LAMPORTS, priority=not self._sol_usd_val)
            self._sol_usd_val, self._sol_usd_ts = int(q["outAmount"]) / 1e6, time.time()
        return self._sol_usd_val

    # ---------- ChainAdapter API ----------
    async def pre_trade_check(self, token: str, amount_native: float) -> tuple[bool, str]:
        try:
            info, pool = await self._resolve(token)
            if self.reject_freeze and info.get("freezeAuthority"):
                return False, "freeze authority set (your tokens can be frozen)"
            if self.reject_mint_auth and info.get("mintAuthority"):
                return False, "mint authority set (supply can be inflated)"
            lamports = int(amount_native * LAMPORTS)
            supply = int(info.get("supply") or 0)
            if pool and token not in self._pools:
                await self._register(token, pool)
            pool = self._pools.get(token)
            if pool and pool.state:
                return await self._check_on_pool(pool, lamports, supply)
            return await self._check_on_jupiter(token, lamports, supply)
        except RateLimited:
            return False, "check error: jupiter rate limit"
        except Exception as e:
            return False, f"check error: {e}"

    async def _check_on_pool(self, pool: _Pool, lamports: int, supply: int) -> tuple[bool, str]:
        st, fee = pool.state, self.fee_bps[pool.venue]
        out = st.buy_out(lamports, fee)
        if out <= 0:
            return False, f"{pool.venue}: pool cannot fill"
        mc_note = ""
        if self.max_mc_usd:
            if not supply:
                return False, "market cap unknown"
            mc_usd = supply * st.lamports_per_raw() / LAMPORTS * await self._sol_usd()
            if mc_usd > self.max_mc_usd:
                return False, f"market cap ${mc_usd:,.0f} > ${self.max_mc_usd:,.0f}"
            mc_note = f", mc ${mc_usd:,.0f}"
        # true round trip: sell back into the pool AFTER our buy (fees both ways)
        loss = 1 - st.after_buy(lamports, fee).sell_out(out, fee) / lamports
        if loss > self.max_roundtrip_loss:
            return False, f"round-trip loss {loss:.1%} > {self.max_roundtrip_loss:.0%} ({pool.venue})"
        return True, f"{pool.venue} round-trip loss {loss:.1%}{mc_note}"

    async def _check_on_jupiter(self, token: str, lamports: int, supply: int) -> tuple[bool, str]:
        buy_q = await self._quote(WSOL, token, lamports, priority=True)
        out = int(buy_q["outAmount"])
        mc_note = ""
        if self.max_mc_usd:
            if not supply or not out:
                return False, "market cap unknown"
            mc_usd = supply * (lamports / out) / LAMPORTS * await self._sol_usd()
            if mc_usd > self.max_mc_usd:
                return False, f"market cap ${mc_usd:,.0f} > ${self.max_mc_usd:,.0f}"
            mc_note = f", mc ${mc_usd:,.0f}"
        sell_q = await self._quote(token, WSOL, out, priority=True)
        # note: this sells into the pool WITHOUT our buy in it, so it overstates the loss
        loss = 1 - int(sell_q["outAmount"]) / lamports
        if loss > self.max_roundtrip_loss:
            return False, f"round-trip loss {loss:.1%} > {self.max_roundtrip_loss:.0%} (jupiter)"
        return True, f"jupiter round-trip loss {loss:.1%}{mc_note}"

    async def buy(self, token: str, amount_native: float) -> Fill:
        lamports = int(amount_native * LAMPORTS)
        try:
            if self.paper:
                # simulate the time a real tx needs to land: other buyers get in first
                await asyncio.sleep(self.paper_delay)
                pool = self._pools.get(token)
                if pool and pool.state:
                    st = pool.state
                    return Fill(True, None, st.buy_out(lamports, self.fee_bps[pool.venue]),
                                amount_native + self.paper_fee, slot=st.slot,
                                model=f"{pool.venue}+{self.paper_delay:g}s")
                q = await self._quote(WSOL, token, lamports, priority=True)
                return Fill(True, None, int(q["outAmount"]), amount_native + self.paper_fee,
                            model=f"jupiter+{self.paper_delay:g}s")
            q = await self._quote(WSOL, token, lamports, priority=True)
            return await self._swap(q, token)
        except Exception as e:
            return Fill(False, None, 0, 0.0, str(e))

    async def quote_exit(self, pos: Position) -> float | None:
        pool = self._pools.get(pos.token)
        if pool and pool.state:
            if not self.hub.connected and time.time() - pool.state.recv_ts > 2:
                try:
                    await self._refresh([pos.token])
                except Exception as e:
                    log.debug("pool refresh failed %s: %s", pos.token, e)
            v = self._pool_value(pos)
            if v is not None:
                return v
        try:
            q = await self._quote(pos.token, WSOL, pos.token_amount)
            return int(q["outAmount"]) / LAMPORTS
        except RateLimited:
            raise
        except Exception as e:
            log.debug("exit quote failed %s: %s", pos.token, e)
            return None

    async def sell(self, pos: Position, slippage_bps: int | None = None) -> Fill:
        try:
            if self.paper:
                await asyncio.sleep(self.paper_delay)
                pool = self._pools.get(pos.token)
                v = self._pool_value(pos)
                if v is not None:
                    return Fill(True, None, pos.token_amount, v - self.paper_fee, slot=pool.state.slot,
                                model=f"{pool.venue}+{self.paper_delay:g}s")
                q = await self._quote(pos.token, WSOL, pos.token_amount, slippage_bps, priority=True)
                return Fill(True, None, pos.token_amount, int(q["outAmount"]) / LAMPORTS - self.paper_fee,
                            model=f"jupiter+{self.paper_delay:g}s")
            q = await self._quote(pos.token, WSOL, pos.token_amount, slippage_bps, priority=True)
            return await self._swap(q, pos.token)
        except Exception as e:
            return Fill(False, None, 0, 0.0, str(e))


def _amount_of(acc: dict) -> int:
    """Token amount from a getMultipleAccounts entry (jsonParsed or base64)."""
    data = acc.get("data")
    if isinstance(data, dict):
        return int(data["parsed"]["info"]["tokenAmount"]["amount"])
    if isinstance(data, list):
        return decode_token_amount(base64.b64decode(data[0]))
    return 0
