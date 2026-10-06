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
from dataclasses import dataclass, field

import aiohttp

log = logging.getLogger("relay")

API = "https://api.relay.link"
RELAY_DEPOSITORY = "99vQwtBwYtrqqD9YSXbdum3KBdxPAVxYTaQ3cfnJSrN2"
RELAY_SOLVER = "F7p3dFrjRTbtRp8FRF6qHLomXbKRBzpvBLjtQcfcgmNe"
SOLANA_CHAIN_ID = 792703809                      # Relay's id for Solana
USDC_SOL = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
CHAIN_NAMES = {SOLANA_CHAIN_ID: "solana", 4663: "robinhood", 56: "bsc", 1: "ethereum", 8453: "base"}
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


class RelayClient:
    """GET /requests/v3 (needs an API key) and POST /price. Never raises on HTTP errors:
    returns None and logs, so the bot keeps running."""

    def __init__(self, session: aiohttp.ClientSession, api_key: str, base: str = API):
        self.session, self.key, self.base = session, api_key, base.rstrip("/")
        self._warned = False
        self._pause_until = 0.0

    @property
    def has_key(self) -> bool:
        return bool(self.key)

    def _headers(self):
        return {"x-api-key": self.key} if self.key else {}

    async def _wait(self):
        if self._pause_until > time.time():
            await asyncio.sleep(self._pause_until - time.time())

    async def requests(self, **params) -> list[dict] | None:
        if not self.key:
            if not self._warned:
                log.warning("RELAY_API_KEY is not set: cross-chain trades are detected and logged as "
                            "leader_events, but NOT resolved (token/amounts/prices). Set the key and restart.")
                self._warned = True
            return None
        await self._wait()
        try:
            async with self.session.get(f"{self.base}/requests/v3", params=params, headers=self._headers()) as r:
                if r.status == 429:
                    self._pause_until = time.time() + 10
                    log.warning("Relay rate limit (429) - pausing 10 s")
                    return None
                j = await r.json(content_type=None)
                if r.status != 200:
                    log.warning("Relay /requests/v3 %s: %s", r.status, str(j)[:200])
                    return None
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
            log.warning("Relay /requests/v3 failed: %s", type(e).__name__)
            return None
        return j.get("requests") or []

    async def price(self, body: dict) -> dict | None:
        await self._wait()
        try:
            async with self.session.post(f"{self.base}/price", json=body, headers=self._headers()) as r:
                if r.status == 429:
                    self._pause_until = time.time() + 10
                    return None
                j = await r.json(content_type=None)
                if r.status != 200:
                    log.info("Relay /price %s: %s", r.status, str(j)[:200])
                    return None
                return j
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
            log.warning("Relay /price failed: %s", type(e).__name__)
            return None
