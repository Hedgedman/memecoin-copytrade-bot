"""Cross-chain watch-only (phase 1), on recorded data, no network: python -m tests.test_xchain

Solana txs, /price and /requests/v3 responses are real recordings (see the fixtures' _note)."""
import asyncio
import json
import logging
import os
import tempfile
import time

from bot.chains.solana import SolanaAdapter
from bot.store import Store
from bot.xchain.relay import RelayClient, memo_request_ids, parse_price, parse_request, relay_tx_kind
from bot.xchain.watcher import XchainWatcher

W = "Leader-A"
EVM = "0x0000000000000000000000004c65616465722d41"
FIX = os.path.join(os.path.dirname(__file__), "fixtures")


def load(name):
    with open(os.path.join(FIX, name)) as f:
        return json.load(f)


DEP_RH, DEP_BSC = load("tx_relay_deposit_rh_Leader-A.json"), load("tx_relay_deposit_bsc_Leader-A.json")
PAYOUT = load("tx_relay_payout_rh_Leader-A.json")
REQS = load("relay_v3_Leader-A.json")["requests"]      # rh buy, bsc buy, rh sell, refund, WETH + USDC bridge
PRICE = {"bsc": load("relay_price_bsc.json"), "robinhood": load("relay_price_robinhood.json")}
sig = lambda tx: tx["transaction"]["signatures"][0]


def parser_tests():
    assert relay_tx_kind(DEP_RH, W) == ("deposit", 1_200_000_000)
    assert relay_tx_kind(DEP_BSC, W) == ("deposit", 100_000_000)
    assert relay_tx_kind(PAYOUT, W) == ("payout", 2_082_534_452)
    assert memo_request_ids(PAYOUT)[-1] == REQS[2]["id"]                 # memo = Relay request id
    assert relay_tx_kind(DEP_RH, EVM) is None                            # someone else's deposit
    assert relay_tx_kind(load("tx_v1_usdc_buy_Leader-G.json"), "Leader-G") is None

    buy, bsc, sell, refund, weth, usdc = (parse_request(r) for r in REQS)
    assert (buy.side, buy.chain, buy.chain_id, buy.symbol) == ("buy", "robinhood", 4663, "MININGMART")
    assert buy.token_amount == 28715964887607914656283786 and buy.decimals == 18   # > 64 bits
    assert abs(buy.usd - 1199.8824) < 1e-6 and abs(buy.price_usd - 4.1784e-05) < 1e-8
    assert buy.deposit_tx == sig(DEP_RH) and buy.fill_tx.startswith("0x5e3d89ce") and buy.fill_ts - buy.deposit_ts == 2
    assert (bsc.side, bsc.chain, bsc.symbol, bsc.deposit_tx) == ("buy", "bsc", "ZKBNB", sig(DEP_BSC))
    assert (sell.side, sell.chain, sell.symbol) == ("sell", "robinhood", "PRIORS")
    assert sell.fill_tx == sig(PAYOUT) and abs(sell.token_ui - 205769.6196) < 1e-3
    assert parse_request({"id": "x", "data": {}}) is None
    assert (refund.status, refund.side, refund.chain, refund.token_amount, refund.price_usd) == \
        ("refund", "buy", "ethereum", 0, None)                           # USDC came back, no tokens
    assert (weth.side, weth.chain, weth.symbol) == ("bridge", "robinhood", "WETH")    # not a token exit
    assert (usdc.side, usdc.chain, usdc.symbol) == ("bridge", "base", "USDC")

    amount, usd, impact = parse_price(PRICE["robinhood"])
    assert abs(amount - 28828282.34) < 0.01 and abs(usd - 93.118498) < 1e-6 and impact == -6.87
    assert parse_price({}) is None
    print("parsers: Relay deposit/payout txs, v3 requests (buy/sell/refund/bridge, >64-bit amounts), /price")


def fresh(r, age_s=0):
    """The recorded request as if his deposit happened age_s ago."""
    data = dict(r["data"], inTxs=[dict(r["data"]["inTxs"][0], timestamp=int(time.time() - age_s))])
    return dict(r, data=data)


class FakeRelay:
    """Stands in for RelayClient: answers like /requests/v3 and /price (deposits age_s old)."""
    def __init__(self, key=True, pending_first=0, age_s=0):
        self.has_key, self.calls, self.pending, self.age = key, [], pending_first, age_s

    async def requests(self, **params):
        self.calls.append(params)
        if not self.has_key:
            return None
        if "depositTxHash" in params:
            hits = [r for r in REQS if r["data"]["inTxs"][0]["txHash"] == params["depositTxHash"]]
        elif "fillTxHash" in params:
            hits = [r for r in REQS if r["data"]["outTxs"][0]["txHash"] == params["fillTxHash"]]
        elif "id" in params:
            hits = [r for r in REQS if r["id"] == params["id"]]
        else:
            hits = list(REQS) if params.get("user") == W else []
        hits = [fresh(h, self.age) for h in hits]
        if self.pending > 0:                     # Relay hasn't settled it yet
            self.pending -= 1
            return [dict(h, status="pending") for h in hits]
        return hits

    async def price(self, body):
        self.calls.append({"price": body})
        return PRICE["robinhood"] if body["destinationChainId"] == 4663 else None


def watcher(client, **cfg):
    store = Store(tempfile.mktemp())
    w = XchainWatcher({"leaders": {W: EVM}, "price_delay_s": 0, "resolve_timeout_s": 10, **cfg}, store, client)
    return w, store


async def flow_tests():
    # stream: his deposit -> resolved (after one 'pending' answer) -> recorded -> priced
    client = FakeRelay(pending_first=1)
    w, store = watcher(client)
    w.on_solana_tx(W, sig(DEP_RH), DEP_RH)
    w.on_solana_tx(W, sig(DEP_RH), DEP_RH)             # duplicate notification: resolved once
    await asyncio.sleep(3.5)
    row = store.db.execute("SELECT side, chain, symbol, token_amount, usd, detected_via, price_later_usd, "
                           "price_later_impact_pct FROM xchain_trades").fetchall()
    assert len(row) == 1, row
    side, chain, sym, amount, usd, via, later, impact = row[0]
    assert (side, chain, sym, via) == ("buy", "robinhood", "MININGMART", "stream")
    assert int(amount) == 28715964887607914656283786                    # stored losslessly as text
    assert abs(later - 1199.8824 / 28828282.339636777) < 1e-12 and impact == -6.87
    p = [c["price"] for c in client.calls if "price" in c][0]
    assert p["amount"] == "1199882400" and p["recipient"] == EVM.lower() and p["originChainId"] == 792703809
    s = store.db.execute("SELECT chain, side, action, quote_amount, venues FROM signals").fetchall()
    assert s == [("robinhood", "buy", "xchain_buy", 1199.8824, "relay")], s
    ev = store.db.execute("SELECT kind, detail FROM leader_events").fetchall()
    assert ev == [("xchain_deposit", "1200.00 USDC via Relay")], ev

    # payout (solver signs, not him) -> resolved by fillTxHash -> sell; poll later finds nothing new
    w.on_solana_tx(W, sig(PAYOUT), PAYOUT)
    await asyncio.sleep(0.1)
    await w.poll_once(3600)                             # poll returns all 6: the stream's 2 aren't new
    await asyncio.sleep(0.1)
    rows = dict(store.db.execute("SELECT symbol, detected_via FROM xchain_trades").fetchall())
    assert rows == {"MININGMART": "stream", "PRIORS": "stream", "ZKBNB": "poll", "GRAPHFUND": "poll",
                    "WETH": "poll", "USDC": "poll"}, rows
    acts = dict(store.db.execute("SELECT token, action FROM signals WHERE action != 'xchain_buy'").fetchall())
    assert sorted(acts.values()) == ["xchain_bridge", "xchain_bridge", "xchain_sell"], acts
    priced = [c["price"]["destinationCurrency"] for c in client.calls if "price" in c]
    assert len(priced) == 3                             # buys + sell only: no refund, no bridges
    sell_price = [c["price"] for c in client.calls if "price" in c and c["price"]["originChainId"] == 4663][0]
    assert sell_price["originCurrency"] == REQS[2]["data"]["route"]["actual"]["origin"]["inputCurrency"]["currency"]["address"]
    assert store.db.execute("SELECT COUNT(*) FROM signals").fetchone()[0] == 6
    # not a leader / not Relay: ignored
    w.on_solana_tx("someone", sig(DEP_RH), DEP_RH)
    w.on_solana_tx(W, "x", load("tx_v1_usdc_buy_Leader-G.json"))
    assert store.db.execute("SELECT COUNT(*) FROM leader_events").fetchone()[0] == 2
    print("flow: deposit -> pending -> success -> trade + signal + price; payout -> sell; poll dedupes")

    # startup poll finds trades from up to an hour ago: recorded, but no misleading "+15 s" price
    client = FakeRelay(age_s=1800)
    w, store = watcher(client)
    await w.poll_once(3600)
    await asyncio.sleep(0.1)
    assert store.db.execute("SELECT COUNT(*) FROM xchain_trades").fetchone()[0] == 6
    assert not [c for c in client.calls if "price" in c]
    print("late: trades found 30 min after the fact are stored but not priced")

    # no API key: detection still logged, nothing resolved, no crash, clear warning once
    w, store = watcher(FakeRelay(key=False))
    w.on_solana_tx(W, sig(DEP_BSC), DEP_BSC)
    await asyncio.sleep(0.1)
    await w.poll_once(3600)
    assert store.db.execute("SELECT kind FROM leader_events").fetchall() == [("xchain_deposit",)]
    assert store.db.execute("SELECT COUNT(*) FROM xchain_trades").fetchone()[0] == 0
    print("no key: deposit logged as leader_event, not resolved, no crash")


async def client_tests():
    msgs = []

    class H(logging.Handler):
        def emit(self, r): msgs.append(r.getMessage())
    logging.getLogger("relay").addHandler(H())
    c = RelayClient(session=None, api_key="")
    assert await c.requests(user=W) is None and await c.requests(user=W) is None
    assert len([m for m in msgs if "RELAY_API_KEY is not set" in m]) == 1      # warned once, no v2 fallback
    print("client: missing key -> None + one clear warning")


async def nonce_tests():
    w, store = watcher(FakeRelay(), rpc_env={"robinhood": "ROBINHOOD_RPC_URL"})
    w.rpc = {"robinhood": "http://rpc"}
    nonces = iter(["0x4e", "0x4f", "0x4f", "0x51"])

    async def eth(url, method, params):
        return next(nonces)
    w._eth = eth
    w._started = 0
    await w.check_nonces()                                   # baseline 78
    await w.check_nonces()                                   # +1, no Relay sell seen -> flagged
    w.record(W, REQS[2], via="poll")                         # the Relay sell explains it
    await w.check_nonces()                                   # +1 with 1 Relay sell -> fine
    await w.check_nonces()                                   # +3 with 1 Relay sell -> 2 unexplained (1 new)
    ev = [r[0] for r in store.db.execute("SELECT detail FROM leader_events WHERE kind='xchain_evm_tx'")]
    assert ev == ["robinhood: nonce +1, Relay requests 0", "robinhood: nonce +3, Relay requests 1"], ev
    print("nonce check: EVM txs without a Relay request are flagged")


async def adapter_hook_tests():
    store = Store(tempfile.mktemp())
    a = SolanaAdapter({"rpc_http": "http://x", "rpc_ws": "ws://x", "leaders": [W]}, paper=True)
    a.set_event_sink(store.record_event)
    seen = []
    a.set_xchain_sink(lambda leader, s, tx: seen.append((leader, s)))
    q = asyncio.Queue()
    await a._handle(sig(DEP_RH), W, q, 1.0, tx=DEP_RH)                         # deposit: not a swap
    a._on_tx_notification({"signature": sig(PAYOUT), "slot": 1,
                           "transaction": {"transaction": PAYOUT["transaction"], "meta": PAYOUT["meta"]}},
                          {W}, q, 1.0)                                          # payout: he didn't sign
    kinds = [r[0] for r in store.db.execute("SELECT kind FROM leader_events")]
    assert kinds == ["not_a_swap"], kinds                                       # existing event unchanged
    assert seen == [(W, sig(DEP_RH))] and q.empty()
    # the solver payout never names his wallet (only his USDC token account): the leader stream
    # can't see sells -> the watcher's /requests/v3 poll finds them (flow_tests)
    assert W not in [k["pubkey"] for k in PAYOUT["transaction"]["message"]["accountKeys"]]
    a.set_xchain_sink(lambda *x: 1 / 0)                                         # a broken sink can't hurt
    await a._handle(sig(DEP_BSC), W, q, 1.0, tx=DEP_BSC)
    print("adapter: Relay txs reach the watcher; existing events and queue unchanged")


if __name__ == "__main__":
    parser_tests()
    asyncio.run(flow_tests())
    asyncio.run(client_tests())
    asyncio.run(nonce_tests())
    asyncio.run(adapter_hook_tests())
    print("ALL PASSED")
