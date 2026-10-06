"""Cross-chain WATCH-ONLY (phase 1): record a leader's Relay trades on other chains. Never trades.

Detection
  stream: the Solana adapter hands us every leader tx it doesn't copy; Relay deposits (his buys,
          he signs them) are resolved via GET /requests/v3 within seconds.
  poll  : every poll_s, /requests/v3 by his wallets. This is how SELLS are found: the solver's
          USDC payout only touches his USDC token account, not his wallet, so the leader
          stream (accountInclude = wallets) never receives it. Also the safety net for buys.
  nonce : optional, if <CHAIN>_RPC_URL env vars are set: his EVM nonce vs the Relay requests
          we saw from that chain, to notice EVM activity that did NOT go through Relay.
Prices
  his price = USD paid (received) / tokens received (sold), from the Relay request.
  ~price_delay_s after his trade we ask Relay /price for the same size: what WE would get.
Storage: table xchain_trades (+ one signals row per trade: chain=<evm chain>,
action=xchain_buy/xchain_sell/xchain_other, quote_amount = USD, venues='relay').
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections import OrderedDict

import aiohttp

from .relay import (CHAIN_NAMES, FINAL, SOLANA_CHAIN_ID, USDC_SOL, RelayClient, XTrade, memo_request_ids,
                    parse_price, parse_request, relay_tx_kind)

log = logging.getLogger("xchain")


class XchainWatcher:
    def __init__(self, cfg: dict, store, client: RelayClient | None = None):
        self.cfg = cfg
        self.store = store
        self.leaders: dict[str, str] = {k: v.lower() for k, v in (cfg.get("leaders") or {}).items()}
        self.price_delay = float(cfg.get("price_delay_s", 15))
        self.poll_s = float(cfg.get("poll_s", 20))
        self.resolve_timeout = float(cfg.get("resolve_timeout_s", 180))
        self.key = os.environ.get(cfg.get("api_key_env", "RELAY_API_KEY"), "")
        self.rpc = {name: os.environ.get(env, "") for name, env in (cfg.get("rpc_env") or {}).items()}
        self.client = client
        self.session: aiohttp.ClientSession | None = None
        self._tasks: set[asyncio.Task] = set()
        self._resolving: set[str] = set()
        self._seen: OrderedDict[str, None] = OrderedDict()           # Solana sigs already handled
        self._nonce: dict[tuple[str, str], tuple[int, int]] = {}     # (chain, evm) -> (start nonce, last unexplained)
        self._started = time.time()

    # ---------- detection from the Solana stream ----------
    def on_solana_tx(self, leader: str, sig: str, tx: dict | None):
        """Called by the Solana adapter for leader txs it did not copy. Cheap; never raises."""
        try:
            if leader not in self.leaders or not tx:
                return
            kind = relay_tx_kind(tx, leader)
            if not kind or sig in self._seen:
                return
            self._seen[sig] = None
            if len(self._seen) > 5000:
                self._seen.popitem(last=False)
            what, usdc = kind
            self.store.record_event(time.time(), leader, sig, f"xchain_{what}", "", None,
                                    f"{usdc / 1e6:.2f} USDC via Relay")
            log.info("xchain %s by %s: %.2f USDC (%s)", what, leader[:8], usdc / 1e6, sig[:16])
            self._spawn(self._resolve(leader, sig, what, memo_request_ids(tx) if what == "payout" else []))
        except Exception as e:                                   # watch-only: never disturb the bot
            log.warning("xchain detection error for %s: %s", sig[:16], e)

    def _spawn(self, coro):
        t = asyncio.ensure_future(coro)
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    async def _resolve(self, leader: str, sig: str, what: str, memo_ids: list[str]):
        if sig in self._resolving:
            return
        self._resolving.add(sig)
        try:
            deadline = time.time() + self.resolve_timeout
            params = {"depositTxHash": sig} if what == "deposit" else {"fillTxHash": sig}
            while time.time() < deadline:
                reqs = await self.client.requests(**params)
                if reqs is None and not self.client.has_key:
                    return                                       # logged once by the client
                if not reqs and memo_ids:                        # payout: the memo is the request id
                    reqs = await self.client.requests(id=memo_ids[-1])
                done = [r for r in reqs or [] if r.get("status") in FINAL]
                if done:
                    for r in done:
                        self.record(leader, r, via="stream")
                    return
                await asyncio.sleep(3)
            log.warning("xchain: Relay request for %s not final after %.0fs", sig[:16], self.resolve_timeout)
            self.store.record_event(time.time(), leader, sig, "xchain_unresolved", "", None,
                                    f"no final Relay request after {self.resolve_timeout:.0f}s")
        finally:
            self._resolving.discard(sig)

    # ---------- recording ----------
    def record(self, leader: str, req: dict, via: str) -> XTrade | None:
        t = parse_request(req)
        if t is None:
            return None
        evm = self.leaders.get(leader, "")
        if not self.store.record_xtrade(t, leader, evm, via):
            return t                                             # already recorded (stream + poll)
        log.info("xchain %s %s %s: %s %s for $%.2f (his price $%.4g) [%s, %s]", t.side, t.chain, t.status,
                 f"{t.token_ui:,.4g}", t.symbol, t.usd, t.price_usd or 0, t.request_id[:12], via)
        if t.status == "success" and t.side in ("buy", "sell"):
            self._spawn(self._price_later(t, leader, evm))
        return t

    async def _price_later(self, t: XTrade, leader: str, evm: str):
        """Relay /price for the same size, ~price_delay_s after his trade: our realistic fill."""
        at = (t.deposit_ts or time.time()) + self.price_delay
        if time.time() - at > 60:            # found late (startup poll): a price now isn't "+15 s"
            return
        await asyncio.sleep(max(0.0, at - time.time()))
        if t.side == "buy":
            usdc = int(round(t.usd * 1e6))
            body = {"user": leader, "recipient": evm, "originChainId": SOLANA_CHAIN_ID,
                    "destinationChainId": t.chain_id, "originCurrency": USDC_SOL,
                    "destinationCurrency": t.token, "amount": str(usdc), "tradeType": "EXACT_INPUT"}
        else:
            body = {"user": evm, "recipient": leader, "originChainId": t.chain_id,
                    "destinationChainId": SOLANA_CHAIN_ID, "originCurrency": t.token,
                    "destinationCurrency": USDC_SOL, "amount": str(t.token_amount), "tradeType": "EXACT_INPUT"}
        p = parse_price(await self.client.price(body) or {})
        if not p:
            self.store.set_xprice(t.request_id, time.time(), None, None, None)
            return
        out_amount, out_usd, impact = p
        if t.side == "buy":                                      # USD per token we'd pay
            price = t.usd / out_amount if out_amount else None
        else:                                                    # USD per token we'd receive
            price = out_amount / t.token_ui if t.token_ui else None
        self.store.set_xprice(t.request_id, time.time(), price, impact, out_usd)
        if price and t.price_usd:
            log.info("xchain %s %s +%.0fs: our price $%.4g vs his $%.4g (%+.1f%%), impact %s%%",
                     t.side, t.symbol, time.time() - (t.deposit_ts or time.time()), price, t.price_usd,
                     (price / t.price_usd - 1) * 100, impact)

    # ---------- safety nets ----------
    async def poll_once(self, lookback_s: float):
        since = int(time.time() - lookback_s)
        for leader, evm in self.leaders.items():
            for user in (leader, evm):
                reqs = await self.client.requests(user=user, startTimestamp=since, limit=50)
                for r in reqs or []:
                    if r.get("status") in FINAL:
                        self.record(leader, r, via="poll")

    async def check_nonces(self):
        """His EVM nonce growth vs the Relay requests we saw from that chain since start."""
        for chain, url in self.rpc.items():
            if not url:
                continue
            for leader, evm in self.leaders.items():
                n = await self._eth(url, "eth_getTransactionCount", [evm, "latest"])
                if n is None:
                    continue
                n = int(n, 16)
                start, last = self._nonce.setdefault((chain, evm), (n, 0))
                relay_txs = self.store.xtrade_count_from(evm, chain, self._started)
                unexplained = (n - start) - relay_txs
                if unexplained > last:
                    log.warning("xchain: %d tx(s) from %s on %s not explained by Relay requests "
                                "(nonce +%d, Relay %d) - check the explorer", unexplained - last, evm[:10],
                                chain, n - start, relay_txs)
                    self.store.record_event(time.time(), leader, "", "xchain_evm_tx", "", None,
                                            f"{chain}: nonce +{n - start}, Relay requests {relay_txs}")
                self._nonce[(chain, evm)] = (start, max(last, unexplained))

    async def _eth(self, url: str, method: str, params: list):
        try:
            async with self.session.post(url, json={"jsonrpc": "2.0", "id": 1, "method": method,
                                                    "params": params}) as r:
                j = await r.json(content_type=None)
            return j.get("result")
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
            log.debug("%s %s failed: %s", method, url.split("//")[-1].split("/")[0], type(e).__name__)
            return None

    async def run(self):
        self._started = time.time()
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))
        if self.client is None:
            self.client = RelayClient(self.session, self.key)
        chains = [c for c, u in self.rpc.items() if u]
        log.info("xchain watch-only for %d leader(s); Relay key %s; EVM nonce check on %s",
                 len(self.leaders), "set" if self.key else "MISSING", chains or "no chains (no RPC env vars)")
        first = True
        try:
            while True:
                try:
                    await self.poll_once(3600 if first else max(self.poll_s * 3, 300))
                    await self.check_nonces()
                except Exception as e:
                    log.warning("xchain poll error: %s", e)
                first = False
                await asyncio.sleep(self.poll_s)
        finally:
            for t in list(self._tasks):
                t.cancel()
            await self.session.close()
