"""Version-1 transactions and stream errors: python -m tests.test_v1

All fixtures are recorded mainnet data (tests/fixtures), no network."""
import asyncio
import inspect
import json
import os
import tempfile

from bot.chains import solana
from bot.chains.solana import (SolanaAdapter, USDC, leader_token_balance, parse_stable_swap, parse_wallet_swap,
                               signing_leader, tx_from_notification, venues_in_tx)
from bot.pricing.pools import find_pool_vaults
from bot.store import Store

W = "Leader-G"
FIX = os.path.join(os.path.dirname(__file__), "fixtures")


def load(name):
    with open(os.path.join(FIX, name)) as f:
        return json.load(f)


def keys_of(tx):
    return tx["transaction"]["message"]["accountKeys"]


def parser_tests():
    buy, sell = load("tx_v1_usdc_buy_Leader-G.json"), load("tx_v1_usdc_sell_Leader-G.json")
    assert buy["version"] == 1 and sell["version"] == 1
    mint = "5ajpbjbjhmEvhTPrNqGhqy5A78vu7M6epbijSbe8TFXJ"

    side, m, amount, usd = parse_stable_swap(buy, W)
    assert (side, m) == ("buy", mint) and abs(usd - 1200) < 0.01, (side, m, usd)
    assert leader_token_balance(buy, W, mint, "post") - leader_token_balance(buy, W, mint, "pre") == amount
    assert parse_wallet_swap(buy, W) is None                 # USDC trade: the SOL parser must stay out
    assert signing_leader(buy, {W}) == W
    assert "pumpswap_amm" in venues_in_tx(buy)
    pool, tv, sv = find_pool_vaults(buy, mint, W)
    k = {x["pubkey"] for x in keys_of(buy)}
    assert pool and tv in k and sv in k

    side, m, amount, usd = parse_stable_swap(sell, W)
    assert (side, m) == ("sell", mint) and abs(usd - 944.342) < 0.01, (side, m, usd)
    assert leader_token_balance(sell, W, mint, "pre") - leader_token_balance(sell, W, mint, "post") == amount
    assert leader_token_balance(sell, W, mint, "pre") > 0
    assert signing_leader(sell, {W}) == W

    # v0 with address lookup tables: loaded addresses are part of accountKeys (source lookupTable)
    alt = load("tx_v0_alt_buy_Leader-G.json")
    assert alt["version"] == 0
    sources = [x.get("source") for x in keys_of(alt)]
    assert sources.count("lookupTable") > 0 and len(alt["meta"]["preBalances"]) == len(sources)
    mine = [b["accountIndex"] for b in alt["meta"]["postTokenBalances"] if b.get("owner") == W]
    assert "lookupTable" in [sources[i] for i in mine]      # one of HIS token accounts is a loaded address
    mint = "CDBhtrQaeBuvB57H6oxKseJVtzG5qpf6eb5GNqgbhYTy"
    side, m, amount, usd = parse_stable_swap(alt, W)
    assert (side, m) == ("buy", mint) and abs(usd - 1700) < 0.01
    pool, tv, sv = find_pool_vaults(alt, mint, W)
    k = {x["pubkey"] for x in keys_of(alt)}
    assert pool and tv in k and sv in k
    print("parsers ok on v1 buy/sell and v0 lookup-table buy")


def adapter(store):
    a = SolanaAdapter({"rpc_http": "http://x", "rpc_ws": "ws://x", "leaders": [W], "min_leader_buy_sol": 0.5},
                      paper=True)
    a.set_event_sink(store.record_event)

    async def sol_usd(refresh=False):
        return 150.0
    a._sol_usd = sol_usd
    return a


def events(store):
    return [tuple(r) for r in store.db.execute("SELECT leader, signature, kind, detail FROM leader_events ORDER BY rowid")]


async def stream_tests():
    # a real v1 notification (maxSupportedTransactionVersion 1) has the shape our parser expects
    n = load("notif_v1_swap.json")
    tx = tx_from_notification(n)
    assert tx and n["transaction"]["version"] == 1
    signer = next(x["pubkey"] for x in keys_of(tx) if x.get("signer") and not x["pubkey"].startswith("AgmLJBMD"))
    assert parse_stable_swap(tx, signer)
    store = Store(tempfile.mktemp())
    a = adapter(store)
    q = asyncio.Queue()
    a._on_tx_notification(n, {signer}, q, 1.0)
    await asyncio.sleep(0.05)
    s = q.get_nowait()
    assert s.leader == signer and s.signature == n["signature"], s

    # error notification -> logged, stored as tx_error, recovered via getTransaction
    err = load("notif_error_unsupported_version.json")
    assert tx_from_notification(err) is None
    buy = load("tx_v1_usdc_buy_Leader-G.json")
    err_for_w = dict(err, signature=buy["transaction"]["signatures"][0])
    store = Store(tempfile.mktemp())
    a = adapter(store)
    asked = []

    async def get_tx(sig, tries=8):
        asked.append(sig)
        return buy
    a._get_tx = get_tx
    q = asyncio.Queue()
    a._on_tx_notification(err_for_w, {W}, q, 1.0)
    a._on_tx_notification(err_for_w, {W}, q, 1.0)          # duplicate: handled once
    await asyncio.sleep(0.05)
    ev = events(store)
    assert ev == [(W, err_for_w["signature"], "tx_error", '{"UnsupportedTransactionVersion": 1}')], ev
    s = q.get_nowait()
    assert s.side == "buy" and s.leader == W and abs(s.quote_amount - 1200 / 150) < 1e-6 and q.empty()
    assert asked == [err_for_w["signature"]]

    # getTransaction can't fetch it either -> still recorded, leader unknown, no signal
    store = Store(tempfile.mktemp())
    a = adapter(store)

    async def no_tx(sig, tries=8):
        return None
    a._get_tx = no_tx
    q = asyncio.Queue()
    a._on_tx_notification(err, {W}, q, 1.0)
    await asyncio.sleep(0.05)
    assert events(store) == [("unknown", err["signature"], "tx_error", '{"UnsupportedTransactionVersion": 1}')]
    assert q.empty()
    print("stream: v1 notification -> signal; error notification -> tx_error event (+ recovery)")


async def request_tests():
    a = SolanaAdapter({"rpc_http": "http://x", "rpc_ws": "ws://x", "leaders": [W]}, paper=True)
    seen = []

    async def rpc(method, params):
        seen.append((method, params))
        return {"slot": 1}
    a._rpc = rpc
    await a._get_tx("sig")
    assert seen[0][1][1]["maxSupportedTransactionVersion"] == 1, seen
    src = inspect.getsource(solana.SolanaAdapter._listen_tx)
    assert '"maxSupportedTransactionVersion": 1' in src and '"maxSupportedTransactionVersion": 0' not in src
    print("getTransaction and transactionSubscribe request version 1")


if __name__ == "__main__":
    parser_tests()
    asyncio.run(stream_tests())
    asyncio.run(request_tests())
    print("ALL PASSED")
