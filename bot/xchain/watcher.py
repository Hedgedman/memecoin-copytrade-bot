"""Cross-chain WATCH-ONLY: record the Solana leaders' Relay trades on other chains. Never trades.

Leaders   every Solana leader (chains.solana.leaders); xchain.leaders = optional manual seeds.
          His EVM address(es) are learned from Relay (recipient of his buys, sender of his sells),
          stored lowercase in xchain_addresses, refreshed from his Relay history every address_refresh_h.
Detection
  stream : the Solana adapter hands us every leader tx it doesn't copy; Relay deposits (his buys,
           he signs them) are resolved via GET /requests/v3 within seconds.
  payouts: his SELL proceeds (Relay solver -> his USDC token account) never name his wallet, so the
           leader stream can't see them. A second Helius transactionSubscribe on the leaders' USDC
           token accounts (payout_stream) pushes them; resolved by fillTxHash / memo request id.
  poll   : safety net, GET /requests/v3 per address, staggered, interval by tier:
             his EVM address(es) (= his sells): hot (we hold a paper position of his) poll_hot_s,
               warm (he traded within warm_window_h) poll_warm_s, else poll_cold_s;
             his Solana wallet (= his buys, e.g. after a network outage): poll_solana_s.
           startTimestamp = previous poll - 2 min, so an outage is caught up on the next poll.
  nonce  : optional (RPC): his EVM nonce vs the Relay requests we saw, every nonce_check_s.
Rate      every Relay call shares one RateLimiter (max_relay_calls_per_min, priorities, 429 back-off).
Prices    his price = USD paid (received) / tokens received (sold), from the Relay request.
          Price ladder: Relay /price for his size at price_offsets_s (default +3/+6/+10/+16 s) after
          we DETECTED his trade (we can't act earlier) -> xchain_prices (delay_s = vs his trade).
          The last step also fills xchain_trades.price_later_* (as before).
          detect_delay_s (xchain_trades) = detected - his deposit tx: how late we see his buys/sells.
Storage: xchain_trades (+ one signals row per trade: chain=<evm chain>, action=xchain_buy/xchain_sell/
xchain_bridge/xchain_other, quote_amount = USD, venues='relay'), xchain_prices, xchain_addresses.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import time
from collections import OrderedDict

import aiohttp

from .relay import (FINAL, RateLimiter, RelayClient, XTrade, memo_request_ids, parse_price, parse_request,
                    price_body, relay_tx_kind, USDC_SOL)

log = logging.getLogger("xchain")

TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
ATA_PROGRAM = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"


def usdc_account(owner: str) -> str:
    """His USDC associated token account (where Relay pays out his sells)."""
    from solders.pubkey import Pubkey
    P = Pubkey.from_string
    return str(Pubkey.find_program_address([bytes(P(owner)), bytes(P(TOKEN_PROGRAM)), bytes(P(USDC_SOL))],
                                           P(ATA_PROGRAM))[0])


def evm_of(t: XTrade) -> str:
    """The EVM side of a Relay request (recipient of a buy, sender of a sell), lowercase."""
    return next((a.lower() for a in (t.recipient, t.sender) if a.startswith("0x")), "")


class XchainWatcher:
    def __init__(self, cfg: dict, store, client: RelayClient | None = None,
                 solana_leaders: list[str] | None = None, ws_url: str = ""):
        self.cfg = cfg
        self.store = store
        seeds = {k: v.lower() for k, v in (cfg.get("leaders") or {}).items()}
        for leader, evm in seeds.items():
            store.upsert_xaddress(leader, evm, time.time(), "config")
        self.sol_leaders: list[str] = list(dict.fromkeys(list(solana_leaders or []) + list(seeds)))
        self.addresses: dict[str, set[str]] = {l: set() for l in self.sol_leaders}
        for leader, evms in store.xaddresses().items():
            if leader in self.addresses:
                self.addresses[leader] |= evms
        self.offsets = sorted(float(x) for x in cfg.get("price_offsets_s", [3, 6, 10, 16]))
        self.resolve_timeout = float(cfg.get("resolve_timeout_s", 180))
        self.hot_s = float(cfg.get("poll_hot_s", 30))
        self.warm_s = float(cfg.get("poll_warm_s", 120))
        self.cold_s = float(cfg.get("poll_cold_s", 900))
        self.warm_window = float(cfg.get("warm_window_h", 72)) * 3600
        self.sol_poll_s = float(cfg.get("poll_solana_s", 600))
        self.refresh_s = float(cfg.get("address_refresh_h", 24)) * 3600
        self.lookback_d = float(cfg.get("address_lookback_d", 14))
        self.nonce_s = float(cfg.get("nonce_check_s", 600))
        self.payout_stream = bool(cfg.get("payout_stream", True)) and bool(ws_url)
        self.ws_url = ws_url
        self.limiter = RateLimiter(int(cfg.get("max_relay_calls_per_min", 100)))
        self.key = os.environ.get(cfg.get("api_key_env", "RELAY_API_KEY"), "")
        # EVM RPC per chain: env var (e.g. Alchemy) first, the public URL as fallback
        public = cfg.get("rpc_public") or {}
        self.rpc: dict[str, str] = {}
        self.rpc_fallback: dict[str, str] = {}
        for name in set(cfg.get("rpc_env") or {}) | set(public):
            env_url = os.environ.get((cfg.get("rpc_env") or {}).get(name, ""), "")
            self.rpc[name] = env_url or public.get(name, "")
            self.rpc_fallback[name] = public.get(name, "") if env_url else ""
        self.client = client
        self.trader = None                                       # XchainPaperTrader, set by Bot if enabled
        self.session: aiohttp.ClientSession | None = None
        self._tasks: set[asyncio.Task] = set()
        self._resolving: set[str] = set()
        self._seen: OrderedDict[str, None] = OrderedDict()           # Solana sigs already handled
        self._nonce: dict[tuple[str, str], tuple[int, int]] = {}     # (chain, evm) -> (start nonce, last unexplained)
        self._last_trade: dict[str, float] = dict(store.xtrade_last_ts())
        self._targets: dict[tuple[str, str, str], dict] = {}         # (kind, leader, address) -> poll state
        self._polling: set[tuple[str, str, str]] = set()
        self._started = time.time()
        for leader in self.sol_leaders:
            self._add_target("sol", leader, leader)
            for evm in self.addresses[leader]:
                self._add_target("evm", leader, evm)

    # ---------- addresses ----------
    def _add_target(self, kind: str, leader: str, address: str, stagger: bool = True):
        key = (kind, leader, address)
        if key not in self._targets:
            # first poll spread over the first ~90 s (looks back 1 h); new addresses: right away
            self._targets[key] = {"last": None, "not_before": time.time() + (random.uniform(0, 90) if stagger else 0)}

    def learn(self, leader: str, evm: str, source: str) -> bool:
        evm = evm.lower()
        if not evm.startswith("0x") or leader not in self.addresses:
            return False
        self.store.upsert_xaddress(leader, evm, time.time(), source)
        if evm in self.addresses[leader]:
            return False
        self.addresses[leader].add(evm)
        self._add_target("evm", leader, evm, stagger=False)
        log.info("xchain: EVM address %s for leader %s (%s)", evm, leader[:8], source)
        return True

    async def refresh_addresses(self):
        """His Relay history (Solana wallet, last address_lookback_d days) -> EVM addresses + last trade."""
        since = int(time.time() - self.lookback_d * 86400)
        found = 0
        for leader in self.sol_leaders:
            reqs = await self._fetch(leader, since, priority=3, max_pages=10)
            for r in reqs or []:
                t = parse_request(r)
                if t is None or t.status != "success" or t.sender != leader:
                    continue
                if t.recipient.startswith("0x"):
                    found += self.learn(leader, t.recipient, "relay_history")
                if t.side in ("buy", "sell") and t.deposit_ts:
                    self._last_trade[leader] = max(self._last_trade.get(leader, 0), t.deposit_ts)
        n = sum(len(v) for v in self.addresses.values())
        log.info("xchain: EVM addresses refreshed: %d leaders, %d addresses (%d new); Relay calls/min %d",
                 sum(bool(v) for v in self.addresses.values()), n, found, self.limiter.used())

    # ---------- detection from the Solana stream ----------
    def on_solana_tx(self, leader: str, sig: str, tx: dict | None, detected_at: float | None = None):
        """Called by the Solana adapter (and the payout stream) for leader txs it did not copy.
        Cheap; never raises."""
        try:
            if leader not in self.addresses or not tx:
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
            self._spawn(self._resolve(leader, sig, what, memo_request_ids(tx) if what == "payout" else [],
                                      detected_at or time.time()))
        except Exception as e:                                   # watch-only: never disturb the bot
            log.warning("xchain detection error for %s: %s", sig[:16], e)

    def _spawn(self, coro):
        t = asyncio.ensure_future(coro)
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    async def _resolve(self, leader: str, sig: str, what: str, memo_ids: list[str], detected_at: float):
        if sig in self._resolving:
            return
        self._resolving.add(sig)
        try:
            deadline = time.time() + self.resolve_timeout
            params = {"depositTxHash": sig} if what == "deposit" else {"fillTxHash": sig}
            while time.time() < deadline:
                reqs = await self.client.requests(priority=1, **params)
                if reqs is None and not self.client.has_key:
                    return                                       # logged once by the client
                if not reqs and memo_ids:                        # payout: the memo is the request id
                    reqs = await self.client.requests(priority=1, id=memo_ids[-1])
                done = [r for r in reqs or [] if r.get("status") in FINAL]
                if done:
                    for r in done:
                        self.record(leader, r, via="stream", detected_at=detected_at)
                    return
                await asyncio.sleep(3)
            log.warning("xchain: Relay request for %s not final after %.0fs", sig[:16], self.resolve_timeout)
            self.store.record_event(time.time(), leader, sig, "xchain_unresolved", "", None,
                                    f"no final Relay request after {self.resolve_timeout:.0f}s")
        finally:
            self._resolving.discard(sig)

    # ---------- payout stream (his sells) ----------
    async def _payout_stream(self):
        import websockets
        from ..chains.solana import tx_from_notification
        accounts = {usdc_account(l): l for l in self.sol_leaders}
        backoff = 1
        while True:
            try:
                async with websockets.connect(self.ws_url, ping_interval=20, max_size=2 ** 23) as ws:
                    await ws.send(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "transactionSubscribe",
                                              "params": [{"accountInclude": list(accounts), "failed": False,
                                                          "vote": False},
                                                         {"commitment": "processed", "encoding": "jsonParsed",
                                                          "transactionDetails": "full",
                                                          "maxSupportedTransactionVersion": 1}]}))
                    ack = json.loads(await asyncio.wait_for(ws.recv(), 15))
                    if "error" in ack:
                        log.warning("xchain payout stream refused (%s): sells found by Relay polling only",
                                    ack["error"])
                        return
                    backoff = 1
                    log.info("xchain: payout stream on %d leaders' USDC accounts (their Relay sells)", len(accounts))
                    async for raw in ws:
                        m = json.loads(raw)
                        if m.get("method") == "transactionNotification":
                            self.on_payout_notification(m["params"]["result"], accounts, tx_from_notification)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("xchain payout stream dropped (%s); reconnecting in %ss (polling covers the gap)",
                            e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    def on_payout_notification(self, res: dict, accounts: dict[str, str], to_tx):
        now = time.time()
        tx = to_tx(res)
        if tx is None:
            return
        sig = res.get("signature") or tx["transaction"].get("signatures", [None])[0]
        for k in tx["transaction"]["message"].get("accountKeys", []):
            leader = accounts.get(k.get("pubkey") if isinstance(k, dict) else k)
            if leader:
                self.on_solana_tx(leader, sig, tx, detected_at=now)

    # ---------- recording ----------
    def record(self, leader: str, req: dict, via: str, detected_at: float | None = None) -> XTrade | None:
        t = parse_request(req)
        if t is None:
            return None
        evm = evm_of(t)
        if evm and leader in (t.sender, t.recipient):
            self.learn(leader, evm, f"relay_{t.side}")
        detected_at = detected_at or time.time()
        if not self.store.record_xtrade(t, leader, evm, via, detected_at):
            return t                                             # already recorded (stream + poll)
        if t.side in ("buy", "sell") and t.deposit_ts:
            self._last_trade[leader] = max(self._last_trade.get(leader, 0), t.deposit_ts)
        delay = detected_at - t.deposit_ts if t.deposit_ts else None
        log.info("xchain %s %s %s by %s: %s %s for $%.2f (his price $%.4g) [%s, %s, seen %s after him]",
                 t.side, t.chain, t.status, leader[:8], f"{t.token_ui:,.4g}", t.symbol, t.usd, t.price_usd or 0,
                 t.request_id[:12], via, f"{delay:.1f}s" if delay is not None else "?")
        if t.status == "success" and t.side in ("buy", "sell"):
            self._spawn(self._price_ladder(t, leader, evm, detected_at))
        if self.trader is not None:                              # paper trading (optional)
            self.trader.on_his_trade(t, leader, evm, detected_at)
        return t

    async def _price_ladder(self, t: XTrade, leader: str, evm: str, detected_at: float):
        """Relay /price for his size at each offset after detection: what WE would have got."""
        his_ts = t.deposit_ts or detected_at
        if detected_at - his_ts > 60:        # found late (poll after an outage): no "+N s" prices
            return
        amount = int(round(t.usd * 1e6)) if t.side == "buy" else t.token_amount
        body = price_body(t.side, leader, evm, t.chain_id, t.token, amount)
        last, steps = None, []
        for off in self.offsets:
            await asyncio.sleep(max(0.0, detected_at + off - time.time()))
            p = parse_price(await self.client.price(body, priority=1) or {})
            now = time.time()
            price = impact = out_usd = gap = None
            if p:
                out_amount, out_usd, impact = p
                if t.side == "buy":                              # USD per token we'd pay
                    price = t.usd / out_amount if out_amount else None
                else:                                            # USD per token we'd receive
                    price = out_amount / t.token_ui if t.token_ui else None
                gap = (price / t.price_usd - 1) * 100 if price and t.price_usd else None
                last = (now, price, impact, out_usd)
            self.store.record_xprice(t.request_id, off, now, now - his_ts, price, gap, impact, out_usd)
            steps.append(f"+{off:g}s {f'{gap:+.1f}%' if gap is not None else 'n/a'}")
        self.store.set_xprice(t.request_id, *(last or (time.time(), None, None, None)))
        log.info("xchain %s %s %s: our price vs his (%s after detection, %.1fs after him): %s",
                 t.side, t.chain, t.symbol, "/".join(f"{o:g}" for o in self.offsets), detected_at - his_ts,
                 ", ".join(steps))

    # ---------- polling (safety net + his sells) ----------
    def _tier(self, kind: str, leader: str) -> tuple[str, float, int]:
        """-> (tier, interval s, Relay priority)."""
        if kind == "sol":
            return "solana", self.sol_poll_s, 3
        if self.trader is not None and leader in self.trader.hot_leaders():
            return "hot", self.hot_s, 1
        if time.time() - self._last_trade.get(leader, 0) < self.warm_window:
            return "warm", self.warm_s, 2
        return "cold", self.cold_s, 3

    def due_targets(self, now: float) -> list[tuple[str, str, str]]:
        due = []
        for key, st in self._targets.items():
            if key in self._polling:
                continue
            _, interval, prio = self._tier(key[0], key[1])
            if now >= max(st["not_before"], (st["last"] or 0) + interval):
                due.append((prio, key))
        return [k for _, k in sorted(due)]

    async def _fetch(self, user: str, since: int, priority: int, max_pages: int = 4) -> list[dict] | None:
        out, cont = [], None
        for _ in range(max_pages):
            params = {"user": user, "startTimestamp": since, "limit": 50}
            if cont:
                params["continuation"] = cont
            page = await self.client.requests_page(priority, **params)
            if page is None:
                return out or None
            reqs, cont = page
            out += reqs
            if not cont or len(reqs) < 50:
                break
        return out

    async def poll_target(self, key: tuple[str, str, str]):
        kind, leader, address = key
        st = self._targets[key]
        self._polling.add(key)
        try:
            started = time.time()
            since = int((st["last"] or started - 3600) - 120)
            reqs = await self._fetch(address, since, self._tier(kind, leader)[2])
            if reqs is None:
                st["not_before"] = time.time() + 60                 # Relay down / no key: retry later
                return
            st["last"] = started
            for r in reqs:
                if r.get("status") in FINAL:
                    self.record(leader, r, via="poll")
        finally:
            self._polling.discard(key)

    async def poll_once(self, lookback_s: float):
        """Poll every target once now (tests / manual catch-up)."""
        for key, st in self._targets.items():
            st["last"] = st["last"] or time.time() - lookback_s + 120
            await self.poll_target(key)

    async def _poll_loop(self):
        while True:
            try:
                for key in self.due_targets(time.time()):
                    self._spawn(self.poll_target(key))
            except Exception as e:
                log.warning("xchain poll error: %s", e)
            await asyncio.sleep(1)

    async def _every(self, seconds: float, fn, first_delay: float = 0):
        await asyncio.sleep(first_delay)
        while True:
            try:
                await fn()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("xchain %s error: %s", fn.__name__, e)
            await asyncio.sleep(seconds)

    async def _stats(self):
        tiers = {}
        for kind, leader, _ in self._targets:
            t = self._tier(kind, leader)[0]
            tiers[t] = tiers.get(t, 0) + 1
        log.info("xchain: Relay calls last min %d (cap %d), total %d, 429s %d; poll targets %s",
                 self.limiter.used(), self.limiter.per_window, self.limiter.total, self.limiter.limited, tiers)

    # ---------- EVM nonce check ----------
    async def check_nonces(self):
        """His EVM nonce growth vs the Relay requests we saw from that chain since start."""
        for chain, url in self.rpc.items():
            if not url:
                continue
            for leader, evms in self.addresses.items():
                for evm in sorted(evms):
                    n = await self._eth(url, "eth_getTransactionCount", [evm, "latest"])
                    if n is None and self.rpc_fallback.get(chain):
                        n = await self._eth(self.rpc_fallback[chain], "eth_getTransactionCount", [evm, "latest"])
                    if not isinstance(n, str):
                        continue
                    n = int(n, 16)
                    start, last = self._nonce.setdefault((chain, evm), (n, 0))
                    relay_txs = self.store.xtrade_count_from(evm, chain, self._started)
                    unexplained = (n - start) - relay_txs
                    if unexplained > last:
                        log.info("xchain: %d tx(s) from %s on %s not explained by Relay requests "
                                 "(nonce +%d, Relay %d)", unexplained - last, evm[:10], chain, n - start, relay_txs)
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
            self.client = RelayClient(self.session, self.key, limiter=self.limiter)
        if self.trader is not None:
            self._spawn(self.trader.run(self.client, self.session, self.rpc, self.rpc_fallback))
        chains = [c for c, u in self.rpc.items() if u]
        log.info("xchain watch-only for %d leaders (%d known EVM addresses); Relay key %s, max %d calls/min; "
                 "payout stream %s; EVM RPC on %s", len(self.sol_leaders),
                 sum(len(v) for v in self.addresses.values()), "set" if self.key else "MISSING",
                 self.limiter.per_window, "on" if self.payout_stream else "off", chains or "none")
        # full history refresh right away only if a leader has no known address yet (saves ~140 calls/restart)
        first_refresh = 0 if any(not v for v in self.addresses.values()) else 600
        jobs = [self._poll_loop(), self._every(self.refresh_s, self.refresh_addresses, first_delay=first_refresh),
                self._every(300, self._stats, first_delay=300)]
        if chains:
            jobs.append(self._every(self.nonce_s, self.check_nonces, first_delay=30))
        if self.payout_stream:
            jobs.append(self._payout_stream())
        try:
            await asyncio.gather(*jobs)
        finally:
            for t in list(self._tasks):
                t.cancel()
            await self.session.close()
