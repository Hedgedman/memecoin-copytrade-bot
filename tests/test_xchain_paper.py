"""Cross-chain PAPER trading (phase 2), no network: python -m tests.test_xchain_paper

Fake Relay /price (we set the token's USD price) and a fake EVM RPC (balanceOf, eth_getLogs)."""
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
from bot.xchain.relay import XTrade
from bot.xchain.watcher import XchainWatcher
from tests.test_xchain import EVM, REQS, W, fresh

TOK, TOK2, TOK3 = "0x" + "a1" * 20, "0x" + "b2" * 20, "0x" + "c3" * 20
E = 10 ** 18
GAS = 0.05


class FakeRelay:
    def __init__(self):
        self.price_usd = {}                    # token -> USD per token (what Relay would quote)
        self.relay_deposits = set()            # EVM tx hashes that are Relay requests
        self.calls = []

    async def price(self, body, priority=1):
        self.calls.append(body)
        if body["originChainId"] == 792703809:                       # buy: USDC -> token
            p = self.price_usd.get(body["destinationCurrency"])
            if not p:
                return None
            out = int(int(body["amount"]) / 1e6 / p * E)
            fmt = out / E
        else:                                                       # sell: token -> USDC
            p = self.price_usd.get(body["originCurrency"])
            if not p:
                return None
            fmt = int(body["amount"]) / E * p
            out = int(fmt * 1e6)
        return {"fees": {"gas": {"amountUsd": str(GAS)}},
                "details": {"currencyOut": {"amount": str(out), "amountFormatted": str(fmt)},
                            "totalImpact": {"percent": "-1.5"}}}

    async def requests(self, priority=1, **params):
        self.calls.append(params)
        return [{"id": "r"}] if params.get("depositTxHash") in self.relay_deposits else []


class FakeRpc:
    def __init__(self, evms=(EVM,)):
        self.balance = {}                      # token -> his balance (raw)
        self.logs = []
        self.block = 1000
        self.evms = {pad_addr(e) for e in evms}    # EVM addresses of the positions under test

    async def __call__(self, url, method, params):
        if method == "eth_call":
            return hex(self.balance.get(params[0]["to"], 0))
        if method == "eth_blockNumber":
            return hex(self.block)
        if method == "eth_getLogs":
            f = params[0]
            assert f["topics"][0] == TRANSFER and set(f["topics"][1]) <= self.evms and f["address"], f
            assert 0 <= int(f["fromBlock"], 16) <= int(f["toBlock"], 16), f
            return [lg for lg in self.logs if lg["address"] in f["address"]
                    and int(f["fromBlock"], 16) <= int(lg["blockNumber"], 16) <= int(f["toBlock"], 16)]
        if method == "eth_getBlockByNumber":
            return {"timestamp": hex(int(time.time()) - 5)}
        raise AssertionError(method)


def trade(side, usd, tokens_ui, token=TOK, chain="robinhood", age=2.0, status="success", rid=None):
    now = time.time()
    return XTrade(rid or f"req-{side}-{usd}-{now}", status, side, chain, 4663 if chain == "robinhood" else 56,
                  token, "TKN", 18, int(tokens_ui * E), usd, W if side == "buy" else EVM,
                  EVM if side == "buy" else W, deposit_tx=f"dep-{now}", deposit_ts=now - age)


def make(rpc=True, **cfg):
    store = Store(tempfile.mktemp())
    base = {"chains": ["robinhood", "bsc"], "entry_delay_s": 0, "dex_confirm_s": 0, "mark_interval_s": 60,
            "sell_retries": 1}
    t = XchainPaperTrader({**base, **cfg}, store, True, {W: EVM}, kill_switch_file=tempfile.mktemp())
    relay, fake_rpc = FakeRelay(), FakeRpc()
    t.client, t.rpc = relay, ({"robinhood": "rpc://rh", "bsc": "rpc://bsc"} if rpc else {})
    t._eth = fake_rpc
    return t, store, relay, fake_rpc


async def settle(t):
    while t._tasks:
        await asyncio.gather(*list(t._tasks), return_exceptions=True)


def pos(store, token=TOK):
    return store.db.execute("SELECT id, token_amount, cost, status, exit_value, exit_reason, timing FROM positions "
                            "WHERE token=? ORDER BY id DESC", (token,)).fetchone()


async def sizing_tests():
    t, store, relay, _ = make()
    relay.price_usd[TOK] = 0.0011                                # we pay 10% more than him (0.001)
    t.on_his_trade(trade("buy", 1000, 1_000_000), W, EVM, time.time())
    await settle(t)
    pid, amount, cost, status, *_ = pos(store)
    assert status == "open" and abs(cost - (400 + GAS)) < 1e-9, cost              # 0.4 x $1000 + gas
    assert abs(int(amount) / E - 400 / 0.0011) < 1
    f = store.db.execute("SELECT side, reason, his_price_usd, our_price_usd, gap_pct, delay_s, impact_pct "
                         "FROM xchain_fills").fetchone()
    assert f[:2] == ("buy", "open") and abs(f[2] - 0.001) < 1e-12 and abs(f[3] - (400 + GAS) / (400 / 0.0011)) < 1e-12
    assert abs(f[4] - 10.0137) < 0.01 and 1.9 < f[5] < 3 and f[6] == -1.5, f         # gap %, delay s
    # small buy: 0.4 x $100 = $40 < $50 -> skipped
    t.on_his_trade(trade("buy", 100, 100_000), W, EVM, time.time())
    await settle(t)
    assert "our size $40.00 < $50" in store.db.execute("SELECT detail FROM leader_events").fetchall()[-1][0]
    # add-ons until the $2,000 cap: +800 (1200), +800 of 1000 (2000), then capped
    for usd in (2000, 2500, 500):
        t.on_his_trade(trade("buy", usd, usd * 1000), W, EVM, time.time())
        await settle(t)
    pid2, amount, cost, *_ , timing = pos(store)
    assert pid2 == pid and abs(cost - (2000 + 3 * GAS)) < 1e-6, cost                # one position, 3 fills
    assert "cap $2,000/token reached" in store.db.execute("SELECT detail FROM leader_events").fetchall()[-1][0]
    kinds = [r[0] for r in store.db.execute("SELECT reason FROM xchain_fills ORDER BY id")]
    assert kinds == ["open", "add", "add"], kinds
    # ignored: stale, other chains, refunds, bridges
    t.on_his_trade(trade("buy", 1000, 1e6, token=TOK2, age=300), W, EVM, time.time())
    t.on_his_trade(trade("buy", 1000, 1e6, token=TOK2, chain="ethereum"), W, EVM, time.time())
    t.on_his_trade(trade("buy", 1000, 1e6, token=TOK2, status="refund"), W, EVM, time.time())
    b = trade("buy", 1000, 1e6, token=TOK2); b.side = "bridge"
    t.on_his_trade(b, W, EVM, time.time())
    await settle(t)
    assert pos(store, TOK2) is None
    assert "stale: detected" in store.db.execute("SELECT detail FROM leader_events").fetchall()[-1][0]
    print("entries: 0.4x his size + gas, <$50 skipped, add-ons to the $2,000 cap, stale/other chain/refund/bridge ignored")


async def mirror_tests():
    t, store, relay, rpc = make()
    relay.price_usd[TOK] = 0.001
    t.on_his_trade(trade("buy", 1000, 1_000_000), W, EVM, time.time())
    await settle(t)
    ours = int(pos(store)[1])
    # he sells 250k of his 1M: balanceOf after = 750k -> 25%
    rpc.balance[TOK] = 750_000 * E
    relay.price_usd[TOK] = 0.002
    t.on_his_trade(trade("sell", 500, 250_000), W, EVM, time.time())
    await settle(t)
    _, _, cost, status, _, _, timing = pos(store)
    import json
    tm = json.loads(timing)
    assert status == "open" and int(tm["remaining"]) == ours - int(ours * 0.25)
    assert abs(tm["realized_usd"] - (ours * 0.25 / E * 0.002 - GAS)) < 1e-6
    f = store.db.execute("SELECT side, reason, fraction, gap_pct FROM xchain_fills ORDER BY id DESC").fetchone()
    sold = int(ours * 0.25) / E                                   # his price 500 / 250k = $0.002, ours minus gas
    assert f[:3] == ("sell", "partial leader_sold", 0.25) and abs(f[3] - ((sold * 0.002 - GAS) / sold / 0.002 - 1) * 100) < 1e-6
    # he sells the rest: balance 0 -> we close
    rpc.balance[TOK] = 0
    t.on_his_trade(trade("sell", 1500, 750_000), W, EVM, time.time())
    await settle(t)
    _, _, cost, status, exitv, reason, timing = pos(store)
    assert status == "closed" and reason == "leader_sold" and abs(exitv - json.loads(timing)["realized_usd"]) < 1e-9
    assert not t.positions
    # no RPC: fraction from what we tracked of him (bought 1M + 0.5M add, sold 0.3M -> 20%)
    t, store, relay, rpc = make(rpc=False)
    relay.price_usd[TOK] = 0.001
    t.on_his_trade(trade("buy", 1000, 1_000_000), W, EVM, time.time())
    await settle(t)
    t.on_his_trade(trade("buy", 500, 500_000), W, EVM, time.time())
    await settle(t)
    t.on_his_trade(trade("sell", 300, 300_000), W, EVM, time.time())
    await settle(t)
    assert store.db.execute("SELECT fraction FROM xchain_fills WHERE side='sell'").fetchone()[0] == 0.2
    print("exits: he sells 25% -> we sell 25% (balanceOf), rest -> close; no RPC: tracked holding (20%)")


async def dex_tests():
    t, store, relay, rpc = make()
    relay.price_usd[TOK] = 0.001
    t.on_his_trade(trade("buy", 1000, 1_000_000), W, EVM, time.time())
    await settle(t)
    await t.logs_once()                                          # first call: start block
    lg = lambda tx, amt, blk: {"address": TOK, "transactionHash": tx, "logIndex": "0x0", "blockNumber": hex(blk),
                               "topics": [TRANSFER, pad_addr(EVM), pad_addr("0x" + "99" * 20)], "data": hex(amt)}
    relay.relay_deposits.add("0xrelay")
    rpc.logs = [lg("0xrelay", 100_000 * E, 1001), lg("0xdex", 450_000 * E, 1002)]
    rpc.balance[TOK] = 450_000 * E                                # he dumped half of what he had left
    rpc.block = 1005
    await t.logs_once()
    await settle(t)
    await t.logs_once()                                          # same logs again: not re-processed
    await settle(t)
    sells = store.db.execute("SELECT reason, fraction, his_ref, his_price_usd FROM xchain_fills WHERE side='sell'").fetchall()
    assert sells == [("partial leader_sold_dex", 0.5, "0xdex", None)], sells
    ev = store.db.execute("SELECT kind, signature FROM leader_events WHERE kind='xchain_dex_sell'").fetchall()
    assert ev == [("xchain_dex_sell", "0xdex")], ev
    # no RPC for logs: warned once, recorded
    t2, store2, *_ = make(rpc=False)
    t2.positions[("bsc", TOK, W)] = t.positions[("robinhood", TOK, W)]
    t2.positions[("bsc", TOK, W)].chain = "bsc"
    await t2.logs_once(); await t2.logs_once()
    assert store2.db.execute("SELECT COUNT(*) FROM leader_events WHERE kind='xchain_logs_unavailable'").fetchone()[0] == 1
    print("DEX sells: Transfer out without Relay request -> mirrored; Relay transfer ignored; no RPC -> flagged once")


async def stop_and_limits_tests():
    t, store, relay, rpc = make(max_open_positions=2)
    for tok in (TOK, TOK2, TOK3):
        relay.price_usd[tok] = 0.001
        t.on_his_trade(trade("buy", 1000, 1_000_000, token=tok), W, EVM, time.time())
        await settle(t)
    assert pos(store, TOK3) is None and len(t.positions) == 2
    assert "max open xchain positions (2)" in store.db.execute("SELECT detail FROM leader_events").fetchall()[-1][0]
    relay.price_usd[TOK] = 0.00045                                # -55%: mark, no stop
    await t.mark_once(); await settle(t)
    assert pos(store, TOK)[3] == "open"
    relay.price_usd[TOK] = 0.00039                                # -61%: safety stop
    await t.mark_once(); await settle(t)
    _, _, cost, status, exitv, reason, _ = pos(store, TOK)
    assert status == "closed" and reason == "safety_stop" and exitv / cost - 1 < -0.6, (status, reason)
    marks = store.db.execute("SELECT COUNT(*) FROM marks").fetchone()[0]
    assert marks == 4, marks                                     # 2 positions x 2 rounds
    # kill switch blocks new entries
    open(t.kill, "w").close()
    t.on_his_trade(trade("buy", 1000, 1_000_000, token=TOK3), W, EVM, time.time())
    await settle(t)
    os.remove(t.kill)
    assert pos(store, TOK3) is None
    # resume: open xchain positions come back in the trader, not in the Solana resume
    t2 = XchainPaperTrader({"chains": ["robinhood"]}, store, True, {W: EVM})
    assert list(t2.positions) == [("robinhood", TOK2, W)]
    assert store.load_open(True, chains={"solana": None}) == []
    try:
        XchainPaperTrader({}, store, False, {W: EVM})
        raise AssertionError("must refuse paper: false")
    except ValueError:
        pass
    print("safety stop at -60% (marks every round), own max open positions, kill switch, resume, paper-only")


async def wiring_tests():
    def cfg(paper, enabled):
        return {"paper": paper, "db_path": tempfile.mktemp(), "chains": {"solana": {
            "enabled": True, "rpc_http": "http://x", "rpc_ws": "ws://x", "leaders": [W], "buy_amount": 0.5}},
            "risk": {"max_open_positions": 4}, "exits": {},
            "xchain": {"enabled": True, "leaders": {W: EVM}, "paper_trading": {"enabled": enabled}}}
    assert Bot(cfg(True, False)).xchain.trader is None                     # default off
    assert isinstance(Bot(cfg(True, True)).xchain.trader, XchainPaperTrader)
    # watcher -> trader: a recorded Relay buy (real v3 request, fresh timestamp) opens a paper position
    store = Store(tempfile.mktemp())
    t = XchainPaperTrader({"entry_delay_s": 0}, store, True, {W: EVM})
    relay = FakeRelay()
    req = fresh(REQS[0])                                                    # MININGMART on robinhood
    tok = req["data"]["route"]["actual"]["destination"]["outputCurrency"]["currency"]["address"]
    relay.price_usd[tok] = 4.5e-05
    t.client = relay
    w = XchainWatcher({"leaders": {W: EVM}, "price_offsets_s": [0]}, store, relay)
    w.trader = t
    w.record(W, req, via="stream", detected_at=time.time())
    w.record(W, req, via="poll")                                            # duplicate: no second buy
    await settle(t)
    assert store.db.execute("SELECT chain, paper, status FROM positions").fetchall() == [("robinhood", 1, "open")]
    cost = store.db.execute("SELECT cost FROM positions").fetchone()[0]
    assert abs(cost - (0.4 * 1199.8824 + GAS)) < 1e-6, cost
    for task in list(w._tasks):
        task.cancel()
    print("wiring: flag off by default; watcher -> trader opens a position once")
    return store


async def whatif_tests():
    t, store, relay, rpc = make()
    relay.price_usd[TOK] = 0.001
    t.on_his_trade(trade("buy", 1000, 1_000_000), W, EVM, time.time())
    await settle(t)
    relay.price_usd[TOK] = 0.0015
    await t.mark_once()
    rpc.balance[TOK] = 500_000 * E
    relay.price_usd[TOK] = 0.002
    t.on_his_trade(trade("sell", 1000, 500_000), W, EVM, time.time())
    await settle(t)
    relay.price_usd[TOK] = 0.0003
    await t.mark_once(); await settle(t)                          # safety stop on the other half
    db_path = store.db.execute("PRAGMA database_list").fetchone()[2]
    out = subprocess.run([sys.executable, "whatif_xchain.py", "--db", db_path, "--list"], capture_output=True,
                         text=True, cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    assert out.returncode == 0, out.stderr
    actual = store.db.execute("SELECT exit_value - cost FROM positions").fetchone()[0]
    assert f"actual closed PnL ${actual:+,.2f}" in out.stdout, out.stdout
    from whatif_xchain import load, simulate
    data = load(sqlite3.connect(db_path), 0)
    ev = data[0][5]
    mirror = simulate(ev, "mirror", 0.60)
    assert abs(mirror[0] - actual) / 400 < 0.01 and not mirror[1], (mirror, actual)   # replays the bot
    first = simulate(ev, "first_sell", None)
    assert first[0] > mirror[0] and not first[1]                   # all out at 2x beats holding half
    hold = simulate(ev, "hold", None)
    assert hold[1]                                                 # never exits -> censored
    why = subprocess.run([sys.executable, "-c", f"import sys; sys.argv=['why.py', '{TOK}']; "
                          f"import sqlite3; c=sqlite3.connect; sqlite3.connect=lambda p: c('{db_path}'); "
                          f"exec(open('why.py').read())"], capture_output=True, text=True,
                         cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    assert why.returncode == 0 and "xFILL" in why.stdout and "(robinhood)" in why.stdout, why.stdout + why.stderr
    print("whatif_xchain: mirror/-60% replays the bot; first_sell / hold variants; why.py shows xchain fills")


async def main():
    await sizing_tests()
    await mirror_tests()
    await dex_tests()
    await stop_and_limits_tests()
    await wiring_tests()
    await whatif_tests()
    print("ALL PASSED")


if __name__ == "__main__":
    asyncio.run(main())
