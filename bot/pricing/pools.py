"""Pool-based pricing for Solana: read pool accounts directly instead of asking Jupiter.

Supported venues
  pumpfun_curve  - Pump.fun bonding curve: one account holding (virtual) reserves
  pumpswap_amm   - PumpSwap constant-product pool: two vault token accounts

Everything is in raw units (lamports, raw token units), so token decimals never matter.
fee_bps is a config value, not a constant: fee models change. Calibrate with calibrate.py.

Why this exists: every Jupiter quote is an HTTP round trip on a 1 req/s free tier.
A pool account pushed over a websocket costs nothing per update, arrives every time
someone trades, and lets us compute exact fills (incl. price impact) locally.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import struct
import time
from dataclasses import dataclass, replace
from typing import Awaitable, Callable

import websockets
from websockets.exceptions import ConnectionClosed
from solders.pubkey import Pubkey

log = logging.getLogger("pools")

PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
PUMPSWAP_PROGRAM = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
WSOL = "So11111111111111111111111111111111111111112"

_CURVE = struct.Struct("<QQQQQ?")   # after the 8-byte Anchor discriminator
# BondingCurve layout (pump.fun IDL, pump-public-docs, checked Oct 8 2026):
#   8 discriminator | virtual_token_reserves u64 | virtual_quote_reserves u64 | real_token_reserves u64 |
#   real_quote_reserves u64 | token_total_supply u64 | complete bool | creator pubkey (49..81) |
#   is_mayhem_mode bool (81) | is_cashback_coin bool (82) | quote_mint pubkey (83..115) | creator_fee_bps u64 | ...
# Older curves are shorter (no quote_mint): they are SOL-quoted. A zero quote_mint also means SOL.
_QUOTE_MINT_OFF = 83
_WSOL_BYTES = bytes(Pubkey.from_string(WSOL))


def curve_pda(mint: str) -> str:
    pda, _ = Pubkey.find_program_address(
        [b"bonding-curve", bytes(Pubkey.from_string(mint))], Pubkey.from_string(PUMP_PROGRAM))
    return str(pda)


def _ceil_div(a: int, b: int) -> int:
    return -(-a // b)


@dataclass(frozen=True)
class PoolState:
    """Constant-product pool. For the Pump.fun curve, tok_res/sol_res are the VIRTUAL
    reserves (they set the price) and real_* cap what can actually be bought/sold."""
    venue: str
    tok_res: int
    sol_res: int
    real_tok: int | None = None
    real_sol: int | None = None
    complete: bool = False        # curve only: graduated, price moved elsewhere
    slot: int = 0
    recv_ts: float = 0.0
    quote_mint: str | None = None  # curve only: a NON-SOL quote mint (reserves are in that token, not lamports)

    def lamports_per_raw(self) -> float:
        return self.sol_res / self.tok_res

    def buy_out(self, lamports: int, fee_bps: int) -> int:
        """Raw tokens received for spending `lamports` (fee included)."""
        net = lamports * (10_000 - fee_bps) // 10_000
        k = self.sol_res * self.tok_res
        out = self.tok_res - _ceil_div(k, self.sol_res + net)
        if self.real_tok is not None:
            out = min(out, self.real_tok)
        return max(0, out)

    def sell_out(self, tokens: int, fee_bps: int) -> int:
        """Lamports received for selling `tokens` raw units, after fee."""
        k = self.sol_res * self.tok_res
        gross = self.sol_res - _ceil_div(k, self.tok_res + tokens)
        if self.real_sol is not None:
            gross = min(gross, self.real_sol)
        return max(0, gross) * (10_000 - fee_bps) // 10_000

    def after_buy(self, lamports: int, fee_bps: int) -> "PoolState":
        """The pool after a buy of `lamports`. Used for (a) round-trip checks and
        (b) paper mode: the real pool would contain OUR buy too, so exits are valued
        on this 'shadow' pool. Without it, paper sells into a pool that never received
        our SOL and books our own price impact as a loss a second time."""
        net = lamports * (10_000 - fee_bps) // 10_000
        out = self.buy_out(lamports, fee_bps)
        return replace(
            self, tok_res=self.tok_res - out, sol_res=self.sol_res + net,
            real_tok=None if self.real_tok is None else self.real_tok - out,
            real_sol=None if self.real_sol is None else self.real_sol + net)


def curve_quote_mint(data: bytes) -> str | None:
    """The curve's quote mint if it is NOT SOL (base58), else None. Short/legacy accounts and a zero
    quote_mint are SOL-quoted. Verified on the 151-byte curve of 9TPazfktQJR5MyrdXNLy9WixRs9tZFCPtnjxoFfyUhWg
    (quote J3NKxxXZcnNiMjKw9hYb2K4LUxgwB6t1FtPtQVsv3KFr, 8 decimals): its 'SOL' reserves were ~32x off."""
    end = _QUOTE_MINT_OFF + 32
    if len(data) < end:
        return None
    q = data[_QUOTE_MINT_OFF:end]
    if q == b"\0" * 32 or q == _WSOL_BYTES:
        return None
    return str(Pubkey.from_bytes(q))


def decode_curve(data: bytes, slot: int = 0, recv_ts: float = 0.0) -> PoolState:
    if len(data) < 8 + _CURVE.size:
        raise ValueError(f"bonding curve account too short: {len(data)} bytes")
    vtok, vsol, rtok, rsol, _supply, complete = _CURVE.unpack_from(data, 8)
    quote = curve_quote_mint(data)
    if (vtok == 0 or vsol == 0) and not complete:
        raise ValueError("empty curve")
    return PoolState("pumpfun_curve", max(vtok, 1), max(vsol, 1), rtok, rsol, complete, slot, recv_ts, quote)


def decode_token_amount(data: bytes) -> int:
    """`amount` of an SPL / Token-2022 token account (mint 32 | owner 32 | amount 8)."""
    if len(data) < 72:
        raise ValueError(f"token account too short: {len(data)} bytes")
    return struct.unpack_from("<Q", data, 64)[0]


def find_pool_vaults(tx: dict | None, mint: str, leader: str) -> tuple[str, str, str] | None:
    """From a leader's swap tx, find the pool that traded with them:
    an owner (the pool account) holding BOTH a `mint` vault and a WSOL vault whose
    balances moved in opposite directions. Returns (pool, token_vault, sol_vault).
    The pool's program is verified later (getMultipleAccounts), not here."""
    if not tx or not tx.get("meta"):
        return None
    meta = tx["meta"]
    keys = [k["pubkey"] if isinstance(k, dict) else k
            for k in tx["transaction"]["message"]["accountKeys"]]
    acc: dict[int, dict] = {}
    for lst, fld in ((meta.get("preTokenBalances") or [], "pre"),
                     (meta.get("postTokenBalances") or [], "post")):
        for b in lst:
            e = acc.setdefault(b["accountIndex"], {"mint": b["mint"], "owner": b.get("owner"),
                                                   "pre": 0, "post": 0})
            e[fld] = int(b["uiTokenAmount"]["amount"])
    by_owner: dict[str, dict[str, tuple[str, int]]] = {}
    for i, e in acc.items():
        if e["owner"] in (None, leader) or e["mint"] not in (mint, WSOL) or i >= len(keys):
            continue
        by_owner.setdefault(e["owner"], {})[e["mint"]] = (keys[i], e["post"] - e["pre"])
    cands = []
    for owner, m in by_owner.items():
        if mint in m and WSOL in m:
            (tv, td), (sv, sd) = m[mint], m[WSOL]
            if td * sd < 0:
                cands.append((abs(td), owner, tv, sv))
    if not cands:
        return None
    _, owner, tv, sv = max(cands)
    return owner, tv, sv


OnAccount = Callable[[str, bytes | None, int, float], None]


class AccountHub:
    """ONE websocket, many accountSubscribe subscriptions, auto-reconnect.

    on_update(pubkey, data, slot, recv_ts) runs inside the receive loop: keep it fast.
    on_connect() is awaited (as a task) after every (re)connect, so the owner can
    re-fetch snapshots for the time we were disconnected.
    """

    def __init__(self, ws_url: str, on_update: OnAccount,
                 on_connect: Callable[[], Awaitable[None]] | None = None,
                 commitment: str = "processed"):
        self.ws_url, self.on_update, self.on_connect = ws_url, on_update, on_connect
        self.commitment = commitment
        self._wanted: set[str] = set()
        self._sub_to_key: dict[int, str] = {}
        self._key_to_sub: dict[str, int] = {}
        self._pending: dict[int, str] = {}
        self._ws = None
        self._next_id = 1
        self.reconnects = 0

    @property
    def connected(self) -> bool:
        return self._ws is not None

    @property
    def watched(self) -> int:
        return len(self._wanted)

    async def watch(self, key: str) -> None:
        if key in self._wanted:
            return
        self._wanted.add(key)
        if self._ws is not None:
            await self._subscribe(key)

    async def unwatch(self, key: str) -> None:
        self._wanted.discard(key)
        sid = self._key_to_sub.pop(key, None)
        if sid is not None:
            self._sub_to_key.pop(sid, None)
            await self._send("accountUnsubscribe", [sid])

    async def _send(self, method: str, params: list) -> int | None:
        if self._ws is None:
            return None
        rid = self._next_id
        self._next_id += 1
        try:
            await self._ws.send(json.dumps({"jsonrpc": "2.0", "id": rid, "method": method,
                                            "params": params}))
        except ConnectionClosed:
            return None
        return rid

    async def _subscribe(self, key: str) -> None:
        rid = await self._send("accountSubscribe",
                               [key, {"encoding": "base64", "commitment": self.commitment}])
        if rid is not None:
            self._pending[rid] = key

    async def run(self, stop: asyncio.Event) -> None:
        backoff = 1.0
        while not stop.is_set():
            try:
                async with websockets.connect(self.ws_url, ping_interval=20, ping_timeout=20,
                                              max_size=2 ** 22) as ws:
                    self._ws = ws
                    self._sub_to_key.clear(); self._key_to_sub.clear(); self._pending.clear()
                    for key in list(self._wanted):
                        await self._subscribe(key)
                    backoff = 1.0
                    if self.on_connect:
                        asyncio.ensure_future(self.on_connect())
                    async for raw in ws:
                        self._handle(json.loads(raw))
                        if stop.is_set():
                            break
            except (OSError, ConnectionClosed, asyncio.TimeoutError) as e:
                log.warning("account hub disconnected: %s", e)
            except Exception as e:   # never let pricing die silently
                log.exception("account hub error: %s", e)
            finally:
                self._ws = None
            if not stop.is_set():
                self.reconnects += 1
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    def _handle(self, msg: dict) -> None:
        rid = msg.get("id")
        if rid is not None and rid in self._pending:
            key = self._pending.pop(rid)
            if "error" in msg:
                log.error("accountSubscribe failed for %s: %s", key, msg["error"])
            elif key in self._wanted:
                self._sub_to_key[msg["result"]] = key
                self._key_to_sub[key] = msg["result"]
            else:   # unwatched while the subscribe was in flight
                asyncio.ensure_future(self._send("accountUnsubscribe", [msg["result"]]))
            return
        if msg.get("method") != "accountNotification":
            return
        params = msg["params"]
        key = self._sub_to_key.get(params["subscription"])
        if key is None:
            return
        res = params["result"]
        value = res.get("value")
        data = base64.b64decode(value["data"][0]) if value else None
        try:
            self.on_update(key, data, res["context"]["slot"], time.time())
        except Exception as e:
            log.exception("account update handler failed for %s: %s", key, e)
