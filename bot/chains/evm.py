"""EVM adapter (BSC, Robinhood Chain, any EVM chain).

Detection (DEX-agnostic): poll eth_getLogs for ERC-20 Transfer events into or out
of leader wallets, group by tx, and keep only txs SENT BY the leader. That filter
matters: it drops airdrops and the address-poisoning dust we saw earlier.
Bonding-curve buys (four.meme, launchpads) are detected like any other buy.

Execution: Uniswap-V2-style router (PancakeSwap V2 on BSC, Uniswap V2 on
Robinhood Chain). Tokens with no V2 pool (still on a bonding curve, or V3/V4
only) fail the quote and are logged as "no V2 route" -- check those logs to
decide which venue to add next.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import OrderedDict, defaultdict

import aiohttp
from eth_abi import decode, encode
from eth_account import Account
from eth_utils import keccak, to_checksum_address

from ..models import Fill, Position, Signal
from .base import ChainAdapter

log = logging.getLogger("evm")

TRANSFER = "0x" + keccak(text="Transfer(address,address,uint256)").hex()
MAX_UINT = 2 ** 256 - 1


def sel(sig: str) -> bytes:
    return keccak(text=sig)[:4]


def pad(addr: str) -> str:
    return "0x" + "0" * 24 + addr.lower()[2:]


def unpad(topic: str) -> str:
    return "0x" + topic[-40:].lower()


def parse_leader_tx(logs: list[dict], leader: str, tx_value_wei: int, wnative: str):
    """Classify a leader-sent tx from its Transfer logs.

    Returns (side, token, token_amount_raw, native_amount) or None.
    native_amount for buys = msg.value + wrapped native sent; for sells it is
    filled in by the caller from the balance delta.
    """
    leader, wnative = leader.lower(), wnative.lower()
    tin, tout = defaultdict(int), defaultdict(int)
    for lg in logs:
        if len(lg["topics"]) != 3 or lg["topics"][0].lower() != TRANSFER:
            continue
        token = lg["address"].lower()
        amt = int(lg["data"], 16) if lg["data"] not in ("0x", "") else 0
        if unpad(lg["topics"][2]) == leader:
            tin[token] += amt
        if unpad(lg["topics"][1]) == leader:
            tout[token] += amt
    wn_out = tout.pop(wnative, 0) - tin.pop(wnative, 0)
    tin = {t: a for t, a in tin.items() if a > 0}
    tout = {t: a for t, a in tout.items() if a > 0}
    native_spent = (tx_value_wei + max(wn_out, 0)) / 1e18
    if len(tin) == 1 and not tout and native_spent > 0:
        t, a = next(iter(tin.items()))
        return ("buy", t, a, native_spent)
    if len(tout) == 1 and not tin:
        t, a = next(iter(tout.items()))
        return ("sell", t, a, 0.0)
    return None


class EvmAdapter(ChainAdapter):
    def __init__(self, name: str, cfg: dict, paper: bool):
        self.name = name
        self.cfg = cfg
        self.paper = paper
        self.rpc = cfg["rpc_http"]
        self.send_rpc = cfg.get("send_rpc") or self.rpc    # private/builder RPC for sending
        self.chain_id = int(cfg["chain_id"])
        self.router = to_checksum_address(cfg["router_v2"])
        self.leaders = [a.lower() for a in cfg["leaders"]]
        self.min_leader_buy = float(cfg.get("min_leader_buy", 0.1))
        self.slippage = int(cfg.get("slippage_bps", 1500)) / 10_000
        self.max_roundtrip_loss = float(cfg.get("max_roundtrip_loss", 0.08))
        self.poll_s = float(cfg.get("log_poll_s", 0.3))
        self.gas_mult = float(cfg.get("gas_price_multiplier", 1.2))
        self.max_gas_gwei = float(cfg.get("max_gas_price_gwei", 10))
        self.gas_limit_swap = int(cfg.get("gas_limit_swap", 400_000))
        self.confirm_timeout = float(cfg.get("confirm_timeout_s", 30))
        self.acct = None
        key = os.environ.get(cfg.get("private_key_env", f"{name.upper()}_PRIVATE_KEY"))
        if key:
            self.acct = Account.from_key(key)
        elif not paper:
            raise RuntimeError(f"{name}: live mode needs a key in env var {cfg.get('private_key_env')}")
        self.me = self.acct.address if self.acct else "0x" + "0" * 40
        self.wnative = None
        self.session: aiohttp.ClientSession | None = None
        self._nonce: int | None = None
        self._nonce_lock = asyncio.Lock()
        self._approved: set[str] = set()
        self._seen: OrderedDict[str, None] = OrderedDict()

    # ---------- plumbing ----------
    async def start(self):
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))
        cid = int(await self._rpc("eth_chainId", []), 16)
        if cid != self.chain_id:
            raise RuntimeError(f"{self.name}: RPC chain id {cid} != configured {self.chain_id}")
        # Read wrapped-native from the router itself instead of hardcoding it
        self.wnative = to_checksum_address(
            decode(["address"], await self._call(self.router, sel("WETH()")))[0])
        log.info("%s: router %s, wrapped native %s, wallet %s (paper=%s)",
                 self.name, self.router, self.wnative, self.me, self.paper)

    async def close(self):
        if self.session:
            await self.session.close()

    async def _rpc(self, method, params, url=None):
        async with self.session.post(url or self.rpc, json={"jsonrpc": "2.0", "id": 1,
                                                            "method": method, "params": params}) as r:
            j = await r.json(content_type=None)
        if "error" in j:
            raise RuntimeError(f"{method}: {j['error']}")
        return j["result"]

    async def _call(self, to: str, data: bytes, block="latest") -> bytes:
        res = await self._rpc("eth_call", [{"to": to, "data": "0x" + data.hex()}, block])
        return bytes.fromhex(res[2:])

    async def _amounts_out(self, amount_in: int, path: list[str]) -> int:
        data = sel("getAmountsOut(uint256,address[])") + encode(["uint256", "address[]"], [amount_in, path])
        return decode(["uint256[]"], await self._call(self.router, data))[0][-1]

    async def _balance_of(self, token: str, who: str, block="latest") -> int:
        data = sel("balanceOf(address)") + encode(["address"], [to_checksum_address(who)])
        return decode(["uint256"], await self._call(to_checksum_address(token), data, block))[0]

    # ---------- detection ----------
    async def listen(self, queue: asyncio.Queue):
        padded = [pad(a) for a in self.leaders]
        last = None
        while True:
            try:
                head = int(await self._rpc("eth_blockNumber", []), 16)
                if last is None:
                    last = head
                if head > last:
                    frm, to = hex(last + 1), hex(min(head, last + 200))
                    logs_in = await self._rpc("eth_getLogs", [{"fromBlock": frm, "toBlock": to,
                                                              "topics": [TRANSFER, None, padded]}])
                    logs_out = await self._rpc("eth_getLogs", [{"fromBlock": frm, "toBlock": to,
                                                               "topics": [TRANSFER, padded]}])
                    txs = {lg["transactionHash"] for lg in logs_in + logs_out}
                    for h in txs:
                        if h not in self._seen:
                            self._seen[h] = None
                            asyncio.create_task(self._handle(h, queue))
                    while len(self._seen) > 20_000:
                        self._seen.popitem(last=False)
                    last = int(to, 16)
            except Exception as e:
                log.warning("%s log poll error: %s", self.name, e)
                await asyncio.sleep(1)
            await asyncio.sleep(self.poll_s)

    async def _handle(self, txh: str, queue: asyncio.Queue):
        try:
            tx = await self._rpc("eth_getTransactionByHash", [txh])
            if not tx or tx["from"].lower() not in self.leaders:
                return          # not sent by a leader: airdrop, poisoning, transfer-in
            rc = await self._rpc("eth_getTransactionReceipt", [txh])
            if not rc or int(rc["status"], 16) != 1:
                return
            leader = tx["from"].lower()
            parsed = parse_leader_tx(rc["logs"], leader, int(tx["value"], 16), self.wnative)
            if not parsed:
                return
            side, token, amount, native = parsed
            blk = int(rc["blockNumber"], 16)
            if side == "buy" and native < self.min_leader_buy:
                return
            if side == "sell":
                native = await self._native_delta(leader, blk, rc)
            block = await self._rpc("eth_getBlockByNumber", [hex(blk), False])
            await queue.put(Signal(self.name, leader, side, to_checksum_address(token), amount,
                                   native, txh, int(block["timestamp"], 16)))
        except Exception as e:
            log.debug("%s handle %s: %s", self.name, txh, e)

    async def _native_delta(self, who: str, blk: int, rc: dict) -> float:
        """Native received in block `blk` (gas added back). Best effort; 0 if state pruned."""
        try:
            b0 = int(await self._rpc("eth_getBalance", [who, hex(blk - 1)]), 16)
            b1 = int(await self._rpc("eth_getBalance", [who, hex(blk)]), 16)
            gas = int(rc["gasUsed"], 16) * int(rc.get("effectiveGasPrice", "0x0"), 16)
            return (b1 - b0 + gas) / 1e18
        except Exception:
            return 0.0

    # ---------- execution ----------
    async def _gas_price(self) -> int:
        gp = int(await self._rpc("eth_gasPrice", []), 16)
        return min(int(gp * self.gas_mult), int(self.max_gas_gwei * 1e9))

    async def _send(self, to: str, data: bytes, value: int, gas: int) -> tuple[str | None, str | None]:
        async with self._nonce_lock:
            if self._nonce is None:
                self._nonce = int(await self._rpc("eth_getTransactionCount", [self.me, "pending"]), 16)
            tx = {"nonce": self._nonce, "gasPrice": await self._gas_price(), "gas": gas,
                  "to": to_checksum_address(to), "value": value, "data": data, "chainId": self.chain_id}
            signed = Account.sign_transaction(tx, self.acct.key)
            raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction
            try:
                h = await self._rpc("eth_sendRawTransaction", ["0x" + raw.hex().removeprefix("0x")],
                                    url=self.send_rpc)
                self._nonce += 1
                return h, None
            except Exception as e:
                self._nonce = None     # resync next time
                return None, str(e)

    async def _wait(self, h: str) -> dict | None:
        deadline = time.time() + self.confirm_timeout
        while time.time() < deadline:
            rc = await self._rpc("eth_getTransactionReceipt", [h])
            if rc:
                return rc
            await asyncio.sleep(0.25)
        return None

    async def pre_trade_check(self, token: str, amount_native: float) -> tuple[bool, str]:
        try:
            if (await self._rpc("eth_getCode", [token, "latest"])) in ("0x", "0x0"):
                return False, "token has no code"
            wei = int(amount_native * 1e18)
            try:
                out = await self._amounts_out(wei, [self.wnative, token])
            except Exception:
                return False, "no V2 route (bonding curve or V3/V4-only token)"
            back = await self._amounts_out(out, [token, self.wnative])
            loss = 1 - back / wei
            if loss > self.max_roundtrip_loss:
                return False, f"round-trip loss {loss:.1%} > {self.max_roundtrip_loss:.0%}"
            return True, f"round-trip loss {loss:.1%} (transfer taxes NOT included)"
        except Exception as e:
            return False, f"check error: {e}"

    async def buy(self, token: str, amount_native: float) -> Fill:
        try:
            wei = int(amount_native * 1e18)
            quote = await self._amounts_out(wei, [self.wnative, token])
            if self.paper:
                return Fill(True, None, quote, amount_native)
            min_out = int(quote * (1 - self.slippage))
            data = sel("swapExactETHForTokensSupportingFeeOnTransferTokens(uint256,address[],address,uint256)") + \
                encode(["uint256", "address[]", "address", "uint256"],
                       [min_out, [self.wnative, token], self.me, int(time.time()) + 60])
            h, err = await self._send(self.router, data, wei, self.gas_limit_swap)
            if err:
                return Fill(False, None, 0, 0.0, err)
            rc = await self._wait(h)
            if not rc:
                return Fill(False, h, 0, 0.0, "not confirmed in time - may still land, CHECK WALLET")
            if int(rc["status"], 16) != 1:
                return Fill(False, h, 0, 0.0, "reverted")
            got = sum(int(lg["data"], 16) for lg in rc["logs"]
                      if lg["address"].lower() == token.lower() and len(lg["topics"]) == 3
                      and lg["topics"][0].lower() == TRANSFER and unpad(lg["topics"][2]) == self.me.lower())
            gas_cost = int(rc["gasUsed"], 16) * int(rc.get("effectiveGasPrice", "0x0"), 16)
            return Fill(True, h, got, (wei + gas_cost) / 1e18)   # got = actual, after any tax
        except Exception as e:
            return Fill(False, None, 0, 0.0, str(e))

    async def quote_exit(self, pos: Position) -> float | None:
        try:
            return await self._amounts_out(pos.token_amount, [pos.token, self.wnative]) / 1e18
        except Exception:
            return None

    async def _ensure_approval(self, token: str) -> str | None:
        if token in self._approved:
            return None
        data = sel("allowance(address,address)") + encode(["address", "address"], [self.me, self.router])
        allowance = decode(["uint256"], await self._call(token, data))[0]
        if allowance < 2 ** 200:
            data = sel("approve(address,uint256)") + encode(["address", "uint256"], [self.router, MAX_UINT])
            h, err = await self._send(token, data, 0, 100_000)
            if err:
                return f"approve failed: {err}"
            rc = await self._wait(h)
            if not rc or int(rc["status"], 16) != 1:
                return "approve not confirmed"
        self._approved.add(token)
        return None

    async def sell(self, pos: Position, slippage_bps: int | None = None) -> Fill:
        try:
            quote = await self._amounts_out(pos.token_amount, [pos.token, self.wnative])
            if self.paper:
                return Fill(True, None, pos.token_amount, quote / 1e18)
            err = await self._ensure_approval(pos.token)
            if err:
                return Fill(False, None, 0, 0.0, err)
            slip = (slippage_bps / 10_000) if slippage_bps else self.slippage
            data = sel("swapExactTokensForETHSupportingFeeOnTransferTokens(uint256,uint256,address[],address,uint256)") + \
                encode(["uint256", "uint256", "address[]", "address", "uint256"],
                       [pos.token_amount, int(quote * (1 - slip)), [pos.token, self.wnative],
                        self.me, int(time.time()) + 60])
            h, err = await self._send(self.router, data, 0, self.gas_limit_swap)
            if err:
                return Fill(False, None, 0, 0.0, err)
            rc = await self._wait(h)
            if not rc:
                return Fill(False, h, 0, 0.0, "not confirmed in time - may still land, CHECK WALLET")
            if int(rc["status"], 16) != 1:
                return Fill(False, h, 0, 0.0, "reverted (transfer tax or sell block?)")
            received = await self._native_delta(self.me, int(rc["blockNumber"], 16), rc)
            gas_cost = int(rc["gasUsed"], 16) * int(rc.get("effectiveGasPrice", "0x0"), 16) / 1e18
            return Fill(True, h, pos.token_amount, received - gas_cost)   # net of gas
        except Exception as e:
            return Fill(False, None, 0, 0.0, str(e))
