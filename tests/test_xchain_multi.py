"""Cross-chain for ALL leaders (no network): python -m tests.test_xchain_multi

EVM address discovery, poll tiers, Relay rate limiter, payout stream, price ladder, per-leader paper
positions + limits, RPC fallback, whatif/why split by leader."""
import asyncio
import os
import sqlite3
import subprocess
import sys
import tempfile
import time

from bot.main import Bot
from bot.store import Store
from bot.xchain.paper import TRANSFER, XchainPaperTrader, pad_addr
from bot.xchain.relay import CHAIN_NAMES, RateLimiter, parse_request
from bot.xchain.watcher import XchainWatcher, usdc_account
from bot.chains.solana import tx_from_notification
from tests.test_xchain import EVM, PAYOUT, REQS, W, FakeRelay, fresh, sig
from tests.test_xchain_paper import E, GAS, TOK, TOK2, FakeRelay as PriceRelay, FakeRpc, settle, trade

L2 = "Leader-D"
EVM2 = "0x" + "cd" * 20
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def chain_tests():
    assert (CHAIN_NAMES[143], CHAIN_NAMES[999], CHAIN_NAMES[5042], CHAIN_NAMES[1337]) == ("monad", "hyperevm", "arc", "hyperliquid")
    print("chain ids 143/999/5042 -> monad/hyperevm/arc")


async def limiter_tests():
    lim = RateLimiter(per_window=10, window_s=0.3, pause_s=0.2, slow_s=0.4)
    for _ in range(5):
        await lim.acquire(3)                                  # background: 50% of the budget
    t0 = time.monotonic()
    await lim.acquire(0)                                      # our fill: not blocked by background
    assert time.monotonic() - t0 < 0.05
    t0 = time.monotonic()
    await lim.acquire(3)                                      # background waits for the window
    assert time.monotonic() - t0 > 0.2
    lim2 = RateLimiter(per_window=10, window_s=0.3, pause_s=0.2, slow_s=0.4)
    lim2.hit_429()
    t0 = time.monotonic()
    await lim2.acquire(0)                                     # paused for everybody
    assert time.monotonic() - t0 >= 0.19
    for _ in range(4):
        await lim2.acquire(0)
    t0 = time.monotonic()
    await lim2.acquire(0)                                     # half budget (5) while slowed
    assert time.monotonic() - t0 > 0.2 and lim2.limited == 1
    print("rate limiter: priorities share one budget; 429 -> pause, then half rate")


class HistoryRelay(FakeRelay):
    """/requests/v3 by user with pages (continuation)."""
    def __init__(self, pages):
        super().__init__()
        self.pages = pages

    async def requests_page(self, priority=1, **params):
        self.calls.append(params)
        pages = self.pages.get(params.get("user"), [[]])
        i = int(params.get("continuation") or 0)
        return pages[i], (str(i + 1) if i + 1 < len(pages) else None)


async def address_tests():
    store = Store(tempfile.mktemp())
    buy_rh, buy_bsc, sell_rh = REQS[0], REQS[1], REQS[2]
    other = dict(buy_rh, id="x2", recipient=EVM2.upper().replace("0X", "0x"))   # 2nd address, mixed case
    full = [buy_rh, buy_bsc] + [dict(buy_rh, id=f"dup{i}") for i in range(48)]  # a full page (50) -> next page
    pages = {W: [full, [other]]}
    w = XchainWatcher({"leaders": {}, "price_offsets_s": []}, store, HistoryRelay(pages), solana_leaders=[W, L2])
    assert w.addresses == {W: set(), L2: set()}
    await w.refresh_addresses()
    assert w.addresses[W] == {EVM.lower(), EVM2} and w.addresses[L2] == set(), w.addresses
    assert store.xaddresses() == {W: {EVM.lower(), EVM2}}
    assert ("evm", W, EVM2) in w._targets and ("sol", L2, L2) in w._targets
    assert w._last_trade[W] > 0
    # a new watcher starts from the table (no history needed)
    w2 = XchainWatcher({}, store, FakeRelay(), solana_leaders=[W])
    assert w2.addresses[W] == {EVM.lower(), EVM2}
    # learned from his trades: buy recipient / sell sender; never from a request that isn't his
    w3 = XchainWatcher({"price_offsets_s": []}, Store(tempfile.mktemp()), FakeRelay(), solana_leaders=[W])
    w3.record(W, sell_rh, via="poll")
    assert w3.addresses[W] == {sell_rh["sender"].lower()}
    w3.record(W, dict(buy_rh, id="foreign", sender="someone", recipient="0x" + "ee" * 20), via="poll")
    assert "0x" + "ee" * 20 not in w3.addresses[W]
    print("addresses: learned from Relay history (paged) + his trades, lowercase, several per leader, persisted")


async def tier_tests():
    store = Store(tempfile.mktemp())
    w = XchainWatcher({"leaders": {W: EVM}, "poll_hot_s": 30, "poll_warm_s": 120, "poll_cold_s": 900,
                       "poll_solana_s": 600}, store, FakeRelay(), solana_leaders=[W, L2])
    w.learn(L2, EVM2, "test")

    class T:
        hot = set()
        def hot_leaders(self): return self.hot
        def on_his_trade(self, *a): pass
    w.trader = T()
    assert w._tier("evm", W)[:2] == ("cold", 900) and w._tier("sol", W)[:2] == ("solana", 600)
    w._last_trade[W] = time.time() - 3600
    assert w._tier("evm", W)[:2] == ("warm", 120)
    T.hot = {W}
    assert w._tier("evm", W) == ("hot", 30, 1)
    now = time.time()
    for st in w._targets.values():
        st["not_before"], st["last"] = 0, now - 200
    due = w.due_targets(now)
    assert due == [("evm", W, EVM.lower())], due                 # only the hot one is due after 200 s
    w._targets[("evm", L2, EVM2)]["last"] = now - 1000
    assert w.due_targets(now)[0] == ("evm", W, EVM.lower())    # hot first (priority), then cold
    assert ("evm", L2, EVM2) in w.due_targets(now)
    # a poll asks for everything since the previous poll (-2 min): an outage is caught up
    client = HistoryRelay({EVM.lower(): [[fresh(REQS[2])]]})
    w.client = client
    await w.poll_target(("evm", W, EVM.lower()))
    assert client.calls[-1]["startTimestamp"] == int(now - 200 - 120)
    assert store.db.execute("SELECT side, detected_via FROM xchain_trades").fetchall() == [("sell", "poll")]
    print("poll tiers: hot 30 s / warm 120 s / cold 900 s / solana 600 s, hot first, since = last poll - 2 min")


async def payout_stream_tests():
    acct = usdc_account(W)
    assert acct in [k["pubkey"] for k in PAYOUT["transaction"]["message"]["accountKeys"]]
    store = Store(tempfile.mktemp())
    client = FakeRelay(age_s=8)
    w = XchainWatcher({"leaders": {W: EVM}, "price_offsets_s": [0], "resolve_timeout_s": 5}, store, client)
    note = {"signature": sig(PAYOUT), "slot": 1,
            "transaction": {"transaction": PAYOUT["transaction"], "meta": PAYOUT["meta"]}}
    w.on_payout_notification(note, {acct: W}, tx_from_notification)
    w.on_payout_notification(note, {acct: W}, tx_from_notification)       # duplicate: once
    await asyncio.sleep(0.2)
    rows = store.db.execute("SELECT side, detected_via, detect_delay_s FROM xchain_trades").fetchall()
    assert len(rows) == 1 and rows[0][:2] == ("sell", "stream") and 7 < rows[0][2] < 10, rows
    assert {"fillTxHash": sig(PAYOUT)} in client.calls
    for t in list(w._tasks):
        t.cancel()
    print("payout stream: his USDC account -> Relay sell resolved by fillTxHash, detection delay stored")


async def ladder_tests():
    store = Store(tempfile.mktemp())
    client = FakeRelay()
    w = XchainWatcher({"leaders": {W: EVM}, "price_offsets_s": [0.05, 0.1, 0]}, store, client)
    w.record(W, fresh(REQS[0], age_s=4), via="stream", detected_at=time.time())
    await asyncio.sleep(0.3)
    rows = store.db.execute("SELECT offset_s, delay_s, price_usd, gap_pct FROM xchain_prices ORDER BY offset_s").fetchall()
    assert [r[0] for r in rows] == [0, 0.05, 0.1], rows
    assert all(4 <= r[1] < 6 and r[2] and r[3] is not None for r in rows), rows      # < 6: loaded machine
    later, delay = store.db.execute("SELECT price_later_usd, detect_delay_s FROM xchain_trades").fetchone()
    assert later == rows[-1][2] and 3.9 < delay < 6
    print("price ladder: one Relay /price per offset after detection, gap vs his price, delay vs his trade")


def make_multi(**cfg):
    store = Store(tempfile.mktemp())
    base = {"chains": ["robinhood", "bsc"], "entry_delay_s": 0, "dex_confirm_s": 0, "sell_retries": 1}
    t = XchainPaperTrader({**base, **cfg}, store, True, [W, L2], kill_switch_file=tempfile.mktemp())
    relay, rpc = PriceRelay(), FakeRpc()
    t.client, t.rpc, t._eth = relay, {"robinhood": "rpc://rh"}, rpc
    return t, store, relay, rpc


async def paper_multi_tests():
    t, store, relay, rpc = make_multi(max_usd_per_token=600)
    relay.price_usd[TOK] = 0.001
    t.on_his_trade(trade("buy", 1000, 1_000_000), W, EVM, time.time())           # we: $400
    t.on_his_trade(trade("buy", 1000, 1_000_000), L2, EVM2, time.time())         # we: $200 (token cap $600)
    await settle(t)
    assert sorted(k[2] for k in t.positions) == sorted([W, L2])
    assert {k[2]: round(p.timing["bought_usd"]) for k, p in t.positions.items()} == {W: 400, L2: 200}
    assert t.hot_leaders() == {W, L2}
    # L2 sells everything (Relay) -> only L2's position closes
    sell = trade("sell", 1000, 1_000_000)
    t.on_his_trade(sell, L2, EVM2, time.time() - 1)
    await settle(t)
    assert [k[2] for k in t.positions] == [W]
    leaders = dict(store.db.execute("SELECT side || reason, leader FROM xchain_fills WHERE side='sell'").fetchall())
    assert list(leaders.values()) == [L2], leaders
    seen = store.db.execute("SELECT detect_delay_s FROM xchain_fills WHERE side='sell'").fetchone()[0]
    assert 0.9 < seen < 1.1, seen                                  # his sell at now-2 s, seen at now-1 s
    # DEX sell out of W's EVM address only touches W's position
    rpc.balance[TOK] = 0
    await t.logs_once()
    rpc.logs = [{"address": TOK, "transactionHash": "0xd", "logIndex": "0x0", "blockNumber": hex(1001),
                 "topics": [TRANSFER, pad_addr(EVM), pad_addr("0x" + "99" * 20)], "data": hex(1_000_000 * E)}]
    rpc.block = 1005                                               # log at 1001 < head - lag (3)
    await t.logs_once(); await settle(t)
    assert not t.positions
    assert store.db.execute("SELECT leader FROM xchain_fills WHERE reason LIKE '%dex%'").fetchall() == [(W,)]

    # limits: 5 per leader, max open in total
    t, store, relay, _ = make_multi(max_open_positions=7, max_open_per_leader=5)
    toks = ["0x" + f"{i:02x}" * 20 for i in range(1, 10)]
    for tok in toks:
        relay.price_usd[tok] = 0.001
    for tok in toks[:6]:
        t.on_his_trade(trade("buy", 500, 500_000, token=tok), W, EVM, time.time())
    await settle(t)
    assert sum(k[2] == W for k in t.positions) == 5
    for tok in toks[6:]:
        t.on_his_trade(trade("buy", 500, 500_000, token=tok), L2, EVM2, time.time())
    await settle(t)
    assert sum(k[2] == L2 for k in t.positions) == 2 and len(t.positions) == 7
    why = [r[0] for r in store.db.execute("SELECT detail FROM leader_events WHERE kind='xchain_paper_skip'")]
    assert sum("per leader (5)" in d for d in why) == 1 and sum("max open xchain positions (7)" in d for d in why) == 1, why
    print("paper: one position per leader, token cap over all leaders, his sells only close his position, "
          "5/leader + total cap, fills carry leader + detection delay")


async def lagging_node_tests():
    """BSC public RPC: eth_getLogs on a node behind the head -> retried, no warning, nothing lost."""
    t, store, relay, rpc = make_multi(log_fail_warn=8)
    t.rpc = {"bsc": "rpc://public"}
    relay.price_usd[TOK] = 0.001
    t.on_his_trade(trade("buy", 1000, 1_000_000, chain="bsc"), W, EVM, time.time())
    await settle(t)
    await t.logs_once()                                           # cursor at head - 3
    start = t._last_block["bsc"]
    fails = {"n": 0}
    inner = rpc.__call__

    async def flaky(url, method, params):
        if method == "eth_getLogs" and fails["n"] < 2 * 9:        # 9 rounds x (try + retry)
            fails["n"] += 1
            return {"code": -32602, "message": "block range extends beyond current head block"}
        return await inner(url, method, params)
    t._eth = flaky
    sleeps = []
    real_sleep = asyncio.sleep
    asyncio.sleep = lambda s, *a: sleeps.append(s) or real_sleep(0)   # skip the 1 s retry pause
    try:
        rpc.logs = [{"address": TOK, "transactionHash": "0xlag", "logIndex": "0x0", "blockNumber": hex(start + 1),
                     "topics": [TRANSFER, pad_addr(EVM), pad_addr("0x" + "99" * 20)], "data": hex(1_000_000 * E)}]
        rpc.balance[TOK] = 0
        rpc.block += 5
        ev = lambda: [r[0] for r in store.db.execute("SELECT kind FROM leader_events WHERE kind LIKE 'xchain_logs%'")]
        for i in range(7):
            await t.logs_once()
        assert ev() == [] and t._last_block["bsc"] == start and 1.0 in sleeps   # quiet, cursor kept, retried
        await t.logs_once()                                       # 8th failed round: one warning
        assert ev() == ["xchain_logs_failing"]
        await t.logs_once()                                       # 9th: no second warning
        await t.logs_once(); await settle(t)                      # node caught up: the same range is re-read
    finally:
        asyncio.sleep = real_sleep
    assert ev() == ["xchain_logs_failing"] and not t.positions, ev()
    assert store.db.execute("SELECT his_ref FROM xchain_fills WHERE side='sell'").fetchone() == ("0xlag",)
    print("EVM logs: lagging RPC node -> retry, cursor kept, warning only after 8 failed rounds, sell still found")


async def rpc_fallback_tests():
    t, store, relay, rpc = make_multi()
    t.rpc, t.rpc_fallback = {"robinhood": "rpc://alchemy"}, {"robinhood": "rpc://public"}
    used = []

    async def eth(url, method, params):
        used.append(url)
        return None if url == "rpc://alchemy" else hex(42)
    t._eth = eth
    assert await t._balance("robinhood", TOK, EVM) == 42 and used == ["rpc://alchemy", "rpc://public"]
    print("EVM RPC: primary (Alchemy) fails -> public fallback")


async def wiring_tests():
    cfg = {"paper": True, "db_path": tempfile.mktemp(), "chains": {"solana": {
        "enabled": True, "rpc_http": "http://x", "rpc_ws": "ws://x", "leaders": [W, L2], "buy_amount": 0.5}},
        "risk": {"max_open_positions": 4}, "exits": {},
        "xchain": {"enabled": True, "leaders": {W: EVM}, "paper_trading": {"enabled": True}}}
    b = Bot(cfg)
    assert b.xchain.sol_leaders == [W, L2] and b.xchain.trader.leaders == {W, L2}
    assert b.xchain.payout_stream and b.xchain.ws_url == "ws://x"
    assert b.xchain.trader.max_open == 50 and b.xchain.trader.max_per_leader == 5
    b2 = Bot({**cfg, "db_path": tempfile.mktemp(), "xchain": {**cfg["xchain"], "all_solana_leaders": False}})
    assert b2.xchain.sol_leaders == [W]
    print("wiring: every Solana leader watched + paper-traded (defaults 50 open, 5/leader)")


async def report_tests():
    t, store, relay, _ = make_multi()
    db_path = store.db.execute("PRAGMA database_list").fetchone()[2]
    relay.price_usd[TOK], relay.price_usd[TOK2] = 0.001, 0.0015       # L2's copy fills 50% above his price
    t.on_his_trade(trade("buy", 1000, 1_000_000), W, EVM, time.time())
    t.on_his_trade(trade("buy", 1000, 1_000_000, token=TOK2), L2, EVM2, time.time())
    await settle(t)
    store.upsert_xaddress(L2, EVM2, time.time(), "test")
    out = subprocess.run([sys.executable, "whatif_xchain.py", "--db", db_path], capture_output=True, text=True, cwd=ROOT)
    assert out.returncode == 0 and W[:8] in out.stdout and L2[:8] in out.stdout, out.stdout + out.stderr
    out = subprocess.run([sys.executable, "whatif_xchain.py", "--db", db_path, "--leader", L2[:8]],
                         capture_output=True, text=True, cwd=ROOT)
    assert "1 xchain paper positions" in out.stdout and W[:8] not in out.stdout, out.stdout
    out = subprocess.run([sys.executable, "whatif_xchain.py", "--db", db_path], capture_output=True, text=True, cwd=ROOT)
    gap = {l.split()[0]: l.split() for l in out.stdout.splitlines() if l.split() and l.split()[0] in ("10%", "40%", "no")}
    assert gap["no"][2] == "2" and gap["10%"][1] == "1" and gap["40%"][1] == "1" and gap["40%"][5] == "1", gap
    rep = subprocess.run([sys.executable, "-m", "scripts.xchain_report", "--db", db_path], capture_output=True,
                         text=True, cwd=ROOT)
    assert rep.returncode == 0, rep.stderr
    ours = rep.stdout.split("OUR PAPER")[1]
    rows = {l.split()[0]: l.split() for l in ours.splitlines() if l[:8] in (W[:8], L2[:8])}
    assert rows[W[:8]][1:5] == ["robinhood", "default", "1", "0"] and rows[L2[:8]][5] == "+50.0%", rows
    assert "OPEN POSITIONS" in rep.stdout and "far too few" in rep.stdout
    why = subprocess.run([sys.executable, "-c", f"import sys; sys.argv=['why.py', '{EVM2.upper().replace('0X', '0x')}']; "
                          f"import sqlite3; c=sqlite3.connect; sqlite3.connect=lambda p: c('{db_path}'); "
                          f"exec(open('why.py').read())"], capture_output=True, text=True, cwd=ROOT)
    assert why.returncode == 0 and f"belongs to leader {L2}" in why.stdout and "xFILL" in why.stdout \
        and "xchain paper position" in why.stdout, why.stdout + why.stderr
    print("whatif_xchain per leader (--leader) + entry-gap filter; xchain_report per leader/chain; "
          "why.py <EVM address>")


async def main():
    chain_tests()
    await limiter_tests()
    await address_tests()
    await tier_tests()
    await payout_stream_tests()
    await ladder_tests()
    await paper_multi_tests()
    await lagging_node_tests()
    await rpc_fallback_tests()
    await wiring_tests()
    await report_tests()
    print("ALL PASSED")


if __name__ == "__main__":
    asyncio.run(main())
