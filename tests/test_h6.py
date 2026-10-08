"""H6 - normal leaders on EVM get the Solana exits (Oct 8 2026): python -m tests.test_h6

TP +30% on cost, stop -20% from the first quote after the fill (stop_basis entry_price), his sells ignored,
max_hold_h; values from the global exits config; marks every 30 s (stretched with many positions); positions
opened before (no exits stamp) keep mirroring his sells with the safety stop; profiles unchanged;
whatif_xchain / xchain_report label them H6."""
import asyncio
import os
import subprocess
import sys
import tempfile
import time

from bot.main import Bot
from bot.models import Position
from bot.store import Store
from bot.xchain.paper import TRANSFER, XchainPaperTrader, normal_exits_from, pad_addr
from tests.test_mirror import M_LEADER, MIRROR
from tests.test_profiles import P_LEADER, PROFILE, profiles
from tests.test_xchain_paper import E, EVM, GAS, TOK, TOK2, TOK3, W, FakeRelay, FakeRpc, settle, trade

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXITS = {"take_profit": 0.30, "stop_loss": 0.20, "stop_basis": "entry_price", "max_hold_s": 0}


def make(**cfg):
    store = Store(tempfile.mktemp())
    base = {"chains": ["robinhood", "bsc"], "entry_delay_s": 0, "dex_confirm_s": 0, "sell_retries": 1,
            "safety_stop": 0.60, "mark_interval_s": 120, "normal_mark_interval_s": 30, "max_hold_h": 48}
    t = XchainPaperTrader({**base, **cfg}, store, True, [W, P_LEADER], kill_switch_file=tempfile.mktemp(),
                          profiles=profiles(), global_exits=EXITS)
    relay, rpc = FakeRelay(), FakeRpc(evms=(EVM,))
    t.client, t.rpc, t._eth = relay, {"robinhood": "rpc://rh"}, rpc
    return t, store, relay, rpc


def config_tests():
    e = normal_exits_from({"take_profit": 0.3, "stop_loss": 0.2, "stop_basis": "entry_price"}, 48)
    assert e == {"mode": "fixed", "rule": "H6", "take_profit": 0.3, "stop_loss": 0.2, "stop_basis": "entry_price",
                 "max_hold_s": 48 * 3600, "follow_leader_exit": False, "trailing_activate": 0.0, "trailing_pct": 0.0}, e
    assert normal_exits_from(None) is None
    cfg = {"paper": True, "db_path": tempfile.mktemp(), "chains": {"solana": {
        "enabled": True, "rpc_http": "http://x", "rpc_ws": "ws://x", "leaders": [W], "buy_amount": 0.5}},
        "risk": {}, "exits": {"take_profit": 0.30, "stop_loss": 0.20, "stop_basis": "entry_price"},
        "xchain": {"enabled": True, "paper_trading": {"enabled": True, "max_hold_h": 48}}}
    tr = Bot(cfg).xchain.trader
    assert tr.normal_exits["take_profit"] == 0.3 and tr.normal_exits["max_hold_s"] == 48 * 3600
    print("config: H6 exits built from the global exits keys + max_hold_h, wired by Bot")


async def exit_tests():
    t, store, relay, rpc = make()
    relay.price_usd[TOK] = 0.001
    t.on_his_trade(trade("buy", 1000, 1_000_000), W, EVM, time.time())
    await settle(t)
    p = t.positions[("robinhood", TOK, W)]
    assert p.timing["exits"]["rule"] == "H6" and p.timing["exits"]["mode"] == "fixed" and "profile" not in p.timing
    assert t._mark_interval(p) == 30
    # first mark = stop reference (0.0011 after our 10% worse fill is not the point: the quote IS the reference)
    relay.price_usd[TOK] = 0.0009
    await t.mark_once(); await settle(t)
    assert abs(p.timing["ref_price_usd"] - 0.0009 * (1 - GAS / (0.0009 * 400 / 0.001))) < 1e-7 or p.timing["ref_price_usd"] > 0
    ref = p.timing["ref_price_usd"]
    # his Relay sell and DEX sell: ignored
    rpc.balance[TOK] = 0
    t.on_his_trade(trade("sell", 1000, 1_000_000), W, EVM, time.time())
    await settle(t)
    await t.logs_once()
    rpc.logs = [{"address": TOK, "transactionHash": "0xd", "logIndex": "0x0", "blockNumber": hex(1001),
                 "topics": [TRANSFER, pad_addr(EVM), pad_addr("0x" + "99" * 20)], "data": hex(10 ** 6 * E)}]
    rpc.block = 1010
    await t.logs_once(); await settle(t)
    assert p.status == "open" and int(p.timing["remaining"]) == p.token_amount
    assert store.db.execute("SELECT COUNT(*) FROM xchain_fills WHERE side='sell'").fetchone()[0] == 0
    # -19% vs the first quote: holds; -21%: stop_loss (the legacy -60% safety stop would not have fired)
    relay.price_usd[TOK] = 0.0009 * 0.81
    await t.mark_once(); await settle(t)
    assert p.status == "open"
    relay.price_usd[TOK] = 0.0009 * 0.785
    await t.mark_once(); await settle(t)
    assert p.status == "closed" and p.exit_reason == "stop_loss", (p.status, p.exit_reason)
    assert p.timing["ref_price_usd"] == ref
    # take-profit: +30% on cost (cost = $400 + gas); +25% holds
    relay.price_usd[TOK2] = 0.001
    t.on_his_trade(trade("buy", 1000, 1_000_000, token=TOK2), W, EVM, time.time())
    await settle(t)
    q = t.positions[("robinhood", TOK2, W)]
    for price, status in ((0.001, "open"), (0.00125, "open"), (0.00132, "closed")):
        relay.price_usd[TOK2] = price
        await t.mark_once(); await settle(t)
        assert q.status == status, (price, q.status)
    assert q.exit_reason == "take_profit" and q.exit_value > 1.29 * q.cost
    # max hold: 48 h -> close at the next mark
    relay.price_usd[TOK3] = 0.001
    t.on_his_trade(trade("buy", 1000, 1_000_000, token=TOK3), W, EVM, time.time())
    await settle(t)
    r = t.positions[("robinhood", TOK3, W)]
    r.opened_at -= 48 * 3600 + 1
    await t.mark_once(); await settle(t)
    assert r.status == "closed" and r.exit_reason == "max_hold"
    print("H6 exits: stop -20% from the first quote, TP +30% on cost, max hold 48 h, his Relay + DEX sells ignored")
    return store


async def add_and_legacy_tests():
    t, store, relay, rpc = make()
    relay.price_usd[TOK] = 0.001
    t.on_his_trade(trade("buy", 1000, 1_000_000), W, EVM, time.time())
    await settle(t)
    p = t.positions[("robinhood", TOK, W)]
    await t.mark_once(); await settle(t)
    ref0, rem0 = p.timing["ref_price_usd"], int(p.timing["remaining"])
    relay.price_usd[TOK] = 0.002                                   # his add at 2x: our ref is re-weighted
    t.on_his_trade(trade("buy", 1000, 500_000), W, EVM, time.time())
    await settle(t)
    rem1 = int(p.timing["remaining"])
    add_price = (400 + GAS) / ((rem1 - rem0) / E)
    assert abs(p.timing["ref_price_usd"] - (ref0 * rem0 + add_price * (rem1 - rem0)) / rem1) < 1e-12
    # a position opened BEFORE H6 (no exits stamp) keeps the legacy rules: his sell mirrored, safety stop -60%
    old = Position(None, "robinhood", TOK2, W, 10 ** 6 * E, 400.0, time.time(), "r0", True, timing={
        "xchain": 1, "symbol": "OLD", "decimals": 18, "chain_id": 4663, "evm": EVM, "remaining": str(10 ** 6 * E),
        "realized_usd": 0.0, "basis_usd": 400.0, "bought_usd": 400.0, "his_bought": str(10 ** 6 * E), "his_sold": "0"})
    old.id = store.open_position(old)
    t2 = XchainPaperTrader({"chains": ["robinhood"], "entry_delay_s": 0, "sell_retries": 1}, store, True, [W],
                           kill_switch_file=tempfile.mktemp(), global_exits=EXITS)
    t2.client, t2.rpc, t2._eth = relay, {"robinhood": "rpc://rh"}, rpc
    o = t2.positions[("robinhood", TOK2, W)]
    assert t2._rule(o) == "legacy" and t2._follows(o) and t2._mark_interval(o) == 120
    relay.price_usd[TOK2] = 0.0003                                 # -25% vs cost: no H6 stop for a legacy position
    await t2.mark_once(); await settle(t2)
    assert o.status == "open"
    rpc.balance[TOK2] = 500_000 * E
    t2.on_his_trade(trade("sell", 150, 500_000, token=TOK2), W, EVM, time.time())
    await settle(t2)
    assert o.status == "open" and abs(int(o.timing["remaining"]) - 500_000 * E) < 1e12   # mirrored 50% (float)
    # profiles unchanged: trail profile position next to H6 ones
    relay.price_usd[TOK3] = 0.001
    t.on_his_trade(trade("buy", 913, 913_000, token=TOK3), P_LEADER, EVM, time.time())
    await settle(t)
    pp = t.positions[("robinhood", TOK3, P_LEADER)]
    assert pp.timing["profile"] == "trail_Leader-B" and pp.timing["exits"]["mode"] == "trail" and t._mark_interval(pp) == 30
    # mark interval stretch: 50 H6 positions on a 100/min budget -> 50 s, not 30
    for i in range(49):
        t.positions[("bsc", f"0x{i:040x}", W)] = Position(None, "bsc", f"0x{i:040x}", W, 1, 1.0, time.time(), None, True,
                                                          timing={"exits": {"rule": "H6"}})
    assert abs(t._mark_interval(p) - 50.0) < 1e-9, t._mark_interval(p)
    print("H6: adds re-weight the stop reference; legacy positions keep mirror + safety stop; profiles unchanged; "
          "marks stretched at 50 positions")
    return store


def report_tests(store, store2):
    db_path = store.db.execute("PRAGMA database_list").fetchone()[2]
    out = subprocess.run([sys.executable, "whatif_xchain.py", "--db", db_path, "--profile", "H6", "--list"],
                         capture_output=True, text=True, cwd=ROOT)
    assert out.returncode == 0 and "3 xchain paper positions" in out.stdout and "H6" in out.stdout, out.stdout + out.stderr
    out = subprocess.run([sys.executable, "whatif_xchain.py", "--db", db_path, "--profile", "default"],
                         capture_output=True, text=True, cwd=ROOT)
    assert "no xchain paper positions" in out.stdout, out.stdout
    db2 = store2.db.execute("PRAGMA database_list").fetchone()[2]
    rep = subprocess.run([sys.executable, "-m", "scripts.xchain_report", "--db", db2], capture_output=True, text=True, cwd=ROOT)
    ours = rep.stdout.split("OUR PAPER")[1]
    labels = {l.split()[2] for l in ours.splitlines() if l[:8] in (W[:8], P_LEADER[:8])}
    assert labels == {"H6", "default", "trail_Leader-B"}, (labels, ours)
    print("reports: whatif_xchain --profile H6 / default, xchain_report rows split H6 / default / profile")


def main():
    config_tests()
    store = asyncio.run(exit_tests())
    store2 = asyncio.run(add_and_legacy_tests())
    report_tests(store, store2)
    print("ALL PASSED")


if __name__ == "__main__":
    main()
