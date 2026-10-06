"""Relay (relay.link) cross-chain data: parsers + a small API client. Watch-only, never trades.

FOMO cross-chain trades go through Relay:
  buy : the leader signs a Solana tx calling the Relay Depository (DepositToken, USDC in);
        a solver delivers the token to his EVM address ~1-2 s later.
  sell: his EVM wallet deposits the token on the EVM chain; the Relay solver pays USDC to his
        Solana wallet (solver signs, a Memo carries the Relay request id).
Each is one Relay "request", resolved via GET /requests/v3 (depositTxHash / fillTxHash).
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field

import aiohttp

log = logging.getLogger("relay")

API = "https://api.relay.link"
RELAY_DEPOSITORY = "99vQwtBwYtrqqD9YSXbdum3KBdxPAVxYTaQ3cfnJSrN2"
RELAY_SOLVER = "F7p3dFrjRTbtRp8FRF6qHLomXbKRBzpvBLjtQcfcgmNe"
SOLANA_CHAIN_ID = 792703809                      # Relay's id for Solana
USDC_SOL = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
CHAIN_NAMES = {SOLANA_CHAIN_ID: "solana", 4663: "robinhood", 56: "bsc", 1: "ethereum", 8453: "base",
               143: "monad", 999: "hyperevm", 5042: "arc"}     # names as in Relay GET /chains
FINAL = ("success", "refund", "failure")
# Moving these between chains is cash management, not a token trade (live v3: after selling a token
# on an EVM DEX he bridges the WETH/WBNB/USDC back to Solana) -> side "bridge".
QUOTE_SYMBOLS = {"USDC", "USDT", "USDC.E", "DAI", "ETH", "WETH", "BNB", "WBNB"}


def _owner_delta(tx: dict, owner: str, mint: str) -> int:
    meta, d = tx["meta"], 0
    for lst, sign in ((meta.get("preTokenBalances") or [], -1), (meta.get("postTokenBalances") or [], 1)):
        for b in lst:
            if b.get("owner") == owner and b.get("mint") == mint:
                d += sign * int(b["uiTokenAmount"]["amount"])
    return d


def relay_tx_kind(tx: dict | None, wallet: str) -> tuple[str, int] | None:
    """('deposit', usdc_raw) = he paid USDC into the Relay Depository (cross-chain buy);
    ('payout', usdc_raw) = the Relay solver paid him USDC (cross-chain sell proceeds); else None."""
    if not tx or not tx.get("meta") or tx["meta"].get("err") is not None:
        return None
    msg = tx["transaction"]["message"]
    keys = {k["pubkey"]: k for k in msg.get("accountKeys", []) if isinstance(k, dict)}
    programs = {ix.get("programId") for ix in msg.get("instructions", [])}
    usdc = _owner_delta(tx, wallet, USDC_SOL)
    if RELAY_DEPOSITORY in programs and keys.get(wallet, {}).get("signer") and usdc < 0:
        return "deposit", -usdc
    if keys.get(RELAY_SOLVER, {}).get("signer") and usdc > 0:
        return "payout", usdc
    return None


def memo_request_ids(tx: dict) -> list[str]:
    """0x-hex memos in a solver payout (one of them is the Relay request id)."""
    out = []
    for ix in tx["transaction"]["message"].get("instructions", []):
        p = ix.get("parsed")
        if ix.get("program") == "spl-memo" and isinstance(p, str) and p.startswith("0x") and len(p) == 66:
            out.append(p)
    return out


@dataclass
class XTrade:
    request_id: str
    status: str
    side: str                 # buy (Solana -> EVM), sell (EVM -> Solana), bridge (USDC/WETH/..), other
    chain: str                # the non-Solana chain (robinhood, bsc, ethereum, base, chain-<id>)
    chain_id: int
    token: str                # token address on that chain
    symbol: str
    decimals: int
    token_amount: int         # raw; can exceed 64 bits -> stored as text
    usd: float                # USD value of the USDC side (what he paid / received)
    sender: str
    recipient: str
    deposit_tx: str = ""
    deposit_ts: float | None = None
    fill_tx: str = ""
    fill_ts: float | None = None
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def price_usd(self) -> float | None:
        """His effective price per token in USD, incl. Relay fees and price impact."""
        n = self.token_amount / 10 ** self.decimals
        return self.usd / n if n else None

    @property
    def token_ui(self) -> float:
        return self.token_amount / 10 ** self.decimals


def _leg(route: dict, side: str, which: str) -> dict:
    for k in ("actual", "quoted"):                         # actual once filled, quoted before
        v = ((route.get(k) or {}).get(side) or {}).get(which)
        if v and v.get("currency"):
            return v
    return {}


def _usd(leg: dict) -> float:
    try:
        return float(leg.get("amountUsd") or 0)
    except (TypeError, ValueError):
        return 0.0


def parse_request(req: dict) -> XTrade | None:
    """One request from GET /requests/v3 -> XTrade, or None if it isn't a token trade we track."""
    data = req.get("data") or {}
    route = data.get("route") or {}
    cin, cout = _leg(route, "origin", "inputCurrency"), _leg(route, "destination", "outputCurrency")
    if not cin or not cout:
        return None
    ci, co = cin["currency"], cout["currency"]
    if ci.get("chainId") == SOLANA_CHAIN_ID and co.get("chainId") != SOLANA_CHAIN_ID:
        side, tok, quote = "buy", cout, cin
    elif ci.get("chainId") != SOLANA_CHAIN_ID and co.get("chainId") == SOLANA_CHAIN_ID:
        side, tok, quote = "sell", cin, cout
    else:
        side, tok, quote = "other", cout, cin
    c = tok["currency"]
    cid = int(c.get("chainId") or 0)
    if side in ("buy", "sell") and (c.get("symbol") or "").upper() in QUOTE_SYMBOLS:
        side = "bridge"
    status = req.get("status", "")
    ins, outs = data.get("inTxs") or [], data.get("outTxs") or []
    t = XTrade(request_id=req.get("id", ""), status=status, side=side,
               chain=CHAIN_NAMES.get(cid, f"chain-{cid}"), chain_id=cid, token=c.get("address", ""),
               symbol=c.get("symbol", ""), decimals=int(c.get("decimals") or 0),
               # refund: the route still shows the quoted tokens, but he got his USDC back (outTx = refund)
               token_amount=0 if status == "refund" else int(tok.get("amount") or 0),
               usd=_usd(quote) or _usd(tok), sender=req.get("sender") or req.get("user") or "",
               recipient=req.get("recipient", ""), raw=req)
    if ins:
        t.deposit_tx, t.deposit_ts = ins[0].get("txHash", ""), ins[0].get("timestamp")
    if outs:
        t.fill_tx, t.fill_ts = outs[0].get("txHash", ""), outs[0].get("timestamp")
    return t


def parse_price(resp: dict) -> tuple[float, float, float | None] | None:
    """POST /price response -> (output amount in token units, USD value of the output, impact %)."""
    d = (resp or {}).get("details") or {}
    out = d.get("currencyOut") or {}
    try:
        amount = float(out["amountFormatted"])
    except (KeyError, TypeError, ValueError):
        return None
    impact = (d.get("totalImpact") or {}).get("percent")
    return amount, _usd(out), float(impact) if impact not in (None, "") else None


def price_body(side: str, sol_user: str, evm_user: str, chain_id: int, token: str, amount_raw: int) -> dict:
    """POST /price body. buy: amount_raw Solana USDC -> token; sell: amount_raw token -> Solana USDC."""
    if side == "buy":
        return {"user": sol_user, "recipient": evm_user, "originChainId": SOLANA_CHAIN_ID,
                "destinationChainId": chain_id, "originCurrency": USDC_SOL, "destinationCurrency": token,
                "amount": str(amount_raw), "tradeType": "EXACT_INPUT"}
    return {"user": evm_user, "recipient": sol_user, "originChainId": chain_id,
            "destinationChainId": SOLANA_CHAIN_ID, "originCurrency": token, "destinationCurrency": USDC_SOL,
            "amount": str(amount_raw), "tradeType": "EXACT_INPUT"}


@dataclass
class Quote:
    out_raw: int              # output in raw units, after Relay's fees (currencyOut is net of relayer fees)
    out_ui: float             # output in token units (USDC units = USD for sells)
    gas_usd: float            # origin-chain gas, paid on top (SOL on buys, ETH/BNB on sells)
    impact_pct: float | None


def parse_quote(resp: dict) -> Quote | None:
    """POST /price response -> Quote (what we'd get for our own size), None if no route."""
    d = (resp or {}).get("details") or {}
    out = d.get("currencyOut") or {}
    try:
        q = Quote(int(out["amount"]), float(out["amountFormatted"]),
                  _usd(((resp or {}).get("fees") or {}).get("gas") or {}), None)
    except (KeyError, TypeError, ValueError):
        return None
    impact = (d.get("totalImpact") or {}).get("percent")
    q.impact_pct = float(impact) if impact not in (None, "") else None
    return q if q.out_raw > 0 else None


class RateLimiter:
    """One budget for every Relay call (sliding window of window_s). Lower priorities only get part of
    it, so our fills never queue behind background polls:
        0 = our paper fills/quotes   1 = resolving his trades, hot polls   2 = marks, warm polls   3 = background
    On a 429: everything pauses pause_s, then runs at half budget for slow_s."""
    SHARE = (1.0, 0.9, 0.7, 0.5)

    def __init__(self, per_window: int = 100, window_s: float = 60.0, pause_s: float = 30.0, slow_s: float = 300.0):
        self.per_window, self.window, self.pause_s, self.slow_s = per_window, window_s, pause_s, slow_s
        self.calls: deque[float] = deque()
        self.pause_until = self.slow_until = 0.0
        self.total = self.limited = 0

    def _prune(self, now: float):
        while self.calls and now - self.calls[0] >= self.window:
            self.calls.popleft()

    def used(self) -> int:
        self._prune(time.monotonic())
        return len(self.calls)

    def budget(self, now: float) -> float:
        return self.per_window * (0.5 if now < self.slow_until else 1.0)

    async def acquire(self, priority: int = 1):
        share = self.SHARE[max(0, min(priority, len(self.SHARE) - 1))]
        while True:
            now = time.monotonic()
            self._prune(now)
            if now >= self.pause_until and len(self.calls) < self.budget(now) * share:
                self.calls.append(now)
                self.total += 1
                return
            if now < self.pause_until:
                wait = self.pause_until - now
            else:
                wait = self.window - (now - self.calls[0]) if self.calls else 0.05
            await asyncio.sleep(min(max(wait, 0.05), 5.0))

    def hit_429(self):
        now = time.monotonic()
        self.limited += 1
        self.pause_until = now + self.pause_s
        self.slow_until = self.pause_until + self.slow_s


class RelayClient:
    """GET /requests/v3 (needs an API key) and POST /price. Never raises on HTTP errors:
    returns None and logs, so the bot keeps running. Every call goes through one RateLimiter."""

    def __init__(self, session: aiohttp.ClientSession, api_key: str, base: str = API,
                 limiter: RateLimiter | None = None):
        self.session, self.key, self.base = session, api_key, base.rstrip("/")
        self.limiter = limiter or RateLimiter()
        self._warned = False

    @property
    def has_key(self) -> bool:
        return bool(self.key)

    def _headers(self):
        return {"x-api-key": self.key} if self.key else {}

    def _limited(self, what: str):
        self.limiter.hit_429()
        log.warning("Relay rate limit (429) on %s - pausing %.0f s, then half rate for %.0f s",
                    what, self.limiter.pause_s, self.limiter.slow_s)

    async def requests(self, priority: int = 1, **params) -> list[dict] | None:
        page = await self.requests_page(priority, **params)
        return None if page is None else page[0]

    async def requests_page(self, priority: int = 1, **params) -> tuple[list[dict], str | None] | None:
        """One page of GET /requests/v3 -> (requests, continuation token or None)."""
        if not self.key:
            if not self._warned:
                log.warning("RELAY_API_KEY is not set: cross-chain trades are detected and logged as "
                            "leader_events, but NOT resolved (token/amounts/prices). Set the key and restart.")
                self._warned = True
            return None
        await self.limiter.acquire(priority)
        try:
            async with self.session.get(f"{self.base}/requests/v3", params=params, headers=self._headers()) as r:
                if r.status == 429:
                    self._limited("/requests/v3")
                    return None
                j = await r.json(content_type=None)
                if r.status != 200:
                    log.warning("Relay /requests/v3 %s: %s", r.status, str(j)[:200])
                    return None
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
            log.warning("Relay /requests/v3 failed: %s", type(e).__name__)
            return None
        return j.get("requests") or [], j.get("continuation") or None

    async def price(self, body: dict, priority: int = 1) -> dict | None:
        await self.limiter.acquire(priority)
        try:
            async with self.session.post(f"{self.base}/price", json=body, headers=self._headers()) as r:
                if r.status == 429:
                    self._limited("/price")
                    return None
                j = await r.json(content_type=None)
                if r.status != 200:
                    log.info("Relay /price %s: %s", r.status, str(j)[:200])
                    return None
                return j
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
            log.warning("Relay /price failed: %s", type(e).__name__)
            return None
