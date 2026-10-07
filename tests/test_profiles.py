"""Per-leader profiles (paper): python -m tests.test_profiles

trail profile on Solana (sizing in USD, own exits, his sells ignored) and on xchain (sizing, cap incl. adds,
own trail/stop on Relay marks, his Relay/DEX sells not followed); default leaders unchanged."""
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time

from bot.main import Bot
from bot.models import Fill, Position, Signal
from bot.positions import PositionManager
from bot.profiles import load_profiles
from bot.store import Store
from bot.xchain.paper import TRANSFER, XchainPaperTrader, pad_addr
from tests.test_xchain_paper import E, GAS, TOK, TOK2, FakeRelay, FakeRpc, settle, trade

P_LEADER = "Leader-B"
OTHER = "Leader-A"
EVM = "0x4f61815d66ac5dd30f8fef0d642e3946650f8213"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROFILE = {"name": "trail_Leader-B", "mode": "trail", "trailing_activate": 0.5, "trailing_pct": 0.35, "stop_loss": 0.60,
           "target_usd": 200, "his_median_usd": 913, "max_usd_per_token": 1000, "min_usd": 25, "mark_interval_s": 30}


def profiles():
    return load_profiles({"leader_profiles": {P_LEADER: PROFILE}}, paper=True)


def profile_tests():
    p = profiles()[P_LEADER]
    assert abs(p.factor - 200 / 913) < 1e-12
    assert p.size_usd(913) == (200.0, None)
    assert p.size_usd(100)[0] == 0 and "< $25" in p.size_usd(100)[1]                # 21.9 < 25
    assert p.size_usd(9999, already_usd=900) == (100.0, None)                       # cap $1,000 incl. adds
    assert "cap $1,000/token reached" in p.size_usd(913, already_usd=990)[1]
    assert load_profiles({"leader_profiles": {P_LEADER: PROFILE}}, paper=False) == {}   # never in live
    try:
        load_profiles({"leader_profiles": {P_LEADER: {**PROFILE, "mode": "yolo"}}}, paper=True)
        raise AssertionError("unknown mode must fail")
    except ValueError:
        pass
    st = p.stamp()
    assert st["profile"] == "trail_Leader-B" and st["exits"]["take_profit"] == 0 and not st["exits"]["follow_leader_exit"]
    print("profile: factor 200/913, $25 minimum, $1,000 cap incl. adds, paper-only, stamp")


class A:
    def __init__(self, path): self.path, self.i = path, 0
    async def quote_exit(self, p):
        v = self.path[min(self.i, len(self.path) - 1)]; self.i += 1; return v
    async def sell(self, p, slip=None):
        return Fill(True, None, p.token_amount, self.path[min(self.i - 1, len(self.path) - 1)])


async def pm_run(path, timing=None):
    store = Store(tempfile.mktemp())
    glob = {"take_profit": 0.3, "stop_loss": 0.2, "max_hold_s": 0, "stop_basis": "entry_price",
            "trailing_activate": 0, "trailing_pct": 0, "mark_interval_s": 0, "follow_leader_exit": False}
    pm = PositionManager(glob, {"solana": A(path)}, store)
    p = Position(None, "solana", "T", P_LEADER, 1, 1.0, time.time(), None, True, timing=dict(timing or {}))
    p.id = store.open_position(p); pm.add(p)
    for _ in path:
        if p.status == "closed":
            break
        await pm._check(p)
    return p, pm


async def solana_exit_tests():
    stamp = profiles()[P_LEADER].stamp()
    # default exits: TP +30%
    p, _ = await pm_run([1.0, 1.2, 1.35])
    assert p.exit_reason == "take_profit"
    # profile: no TP; trail active after 1.5x, sells 35% below the peak (2.4 * 0.65 = 1.56)
    p, _ = await pm_run([1.0, 1.35, 1.8, 2.4, 2.0, 1.55, 1.0], stamp)
    assert p.exit_reason == "trailing_stop" and p.exit_value == 1.55, (p.exit_reason, p.exit_value)
    # profile: a -50% dip doesn't stop it (global -20% would), -60% does
    p, _ = await pm_run([1.0, 0.5, 0.45, 0.39], stamp)
    assert p.exit_reason == "stop_loss" and p.exit_value == 0.39, (p.exit_reason, p.exit_value)
    p, _ = await pm_run([1.0, 0.75], {})
    assert p.exit_reason == "stop_loss"                                          # default: -20%
    # his sell: ignored for the profile even if the global setting follows the leader
    p, pm = await pm_run([1.0, 1.1], stamp)
    pm.follow_exit = True
    await pm.on_leader_sell("solana", P_LEADER, "T")
    assert p.status == "open"
    # restart: the exits come back from the stored timing
    reloaded = pm.store.load_open(True)[0]
    assert reloaded.timing["exits"]["trailing_pct"] == 0.35 and reloaded.timing["profile"] == "trail_Leader-B"
    print("solana: profile exits per position (no TP, trail 35% after 1.5x, stop -60%), his sell ignored, "
          "default leaders unchanged, survives restart")


class FakeSol:
    """Solana adapter stand-in for Bot.handle_buy."""
    def __init__(self):
        self.bought = []
    async def sol_usd(self): return 200.0
    async def pre_trade_check(self, token, size): return True, "ok"
    async def buy(self, token, size):
        self.bought.append(size)
        return Fill(True, "sig", 1000, size + 0.01, model="pumpswap_amm+0.8s")
    def fill_market_cap(self, token, fill): return 50_000
    async def track(self, token): pass
    async def observe(self, token): pass


async def solana_sizing_tests():
    cfg = {"paper": True, "db_path": tempfile.mktemp(), "chains": {"solana": {
        "enabled": True, "rpc_http": "http://x", "rpc_ws": "ws://x", "leaders": [P_LEADER, OTHER],
        "buy_amount": 2.0, "max_signal_age_s": 999}}, "risk": {"max_open_positions": 10, "reentry_cooldown_s": 0},
        "exits": {}, "leader_profiles": {P_LEADER: PROFILE}}
    b = Bot(cfg)
    a = FakeSol()
    b.adapters["solana"] = a
    sig = lambda leader, tok, sol: Signal("solana", leader, "buy", tok, 10, sol, f"s-{tok}", int(time.time()))
    await b.handle_buy(sig(P_LEADER, "TOKA", 4.565))           # his $913 at $200/SOL -> our $200 = 1 SOL
    await b.handle_buy(sig(P_LEADER, "TOKB", 0.4))             # his $80 -> our $17.5 < $25: skipped
    await b.handle_buy(sig(P_LEADER, "TOKC", 50))              # his $10,000 -> $2,190, capped at $1,000 = 5 SOL
    await b.handle_buy(sig(OTHER, "TOKD", 3.0))                # default leader: buy_amount 2 SOL
    assert [round(x, 6) for x in a.bought] == [1.0, 5.0, 2.0], a.bought
    rows = {tok: json.loads(t) for tok, t in b.store.db.execute("SELECT token, timing FROM positions")}
    assert rows["TOKA"]["profile"] == "trail_Leader-B" and abs(rows["TOKA"]["size_factor"] - 0.219058) < 1e-6
    assert rows["TOKA"]["our_usd"] == 200.0 and rows["TOKA"]["exits"]["stop_loss"] == 0.6
    assert "profile" not in rows["TOKD"] and "exits" not in rows["TOKD"]
    skip = b.store.db.execute("SELECT reason FROM signals WHERE action='skip'").fetchall()
    assert skip == [("profile trail_Leader-B: our size $17.52 < $25",)], skip
    print("solana sizing: his USD x 0.219 (SOL converted), < $25 skipped, $1,000 cap, default leader 2 SOL")


def make_trader():
    store = Store(tempfile.mktemp())
    base = {"chains": ["robinhood", "bsc"], "entry_delay_s": 0, "dex_confirm_s": 0, "sell_retries": 1}
    t = XchainPaperTrader(base, store, True, [P_LEADER, OTHER], kill_switch_file=tempfile.mktemp(),
                          profiles=profiles())
    relay, rpc = FakeRelay(), FakeRpc(evms=(EVM,))
    t.client, t.rpc, t._eth = relay, {"robinhood": "rpc://rh"}, rpc
    return t, store, relay, rpc


async def xchain_tests():
    t, store, relay, rpc = make_trader()
    relay.price_usd[TOK] = 0.001
    t.on_his_trade(trade("buy", 913, 913_000), P_LEADER, EVM, time.time())        # -> $200
    await settle(t)
    p = t.positions[("robinhood", TOK, P_LEADER)]
    assert abs(p.timing["bought_usd"] - 200) < 1e-6 and p.timing["profile"] == "trail_Leader-B"
    assert p.timing["peak_value"] == p.cost and p.timing["exits"]["mode"] == "trail"
    # adds: up to $1,000 incl. his adds, then capped
    t.on_his_trade(trade("buy", 3000, 3_000_000), P_LEADER, EVM, time.time())     # $657
    t.on_his_trade(trade("buy", 3000, 3_000_000), P_LEADER, EVM, time.time())     # only $143 left
    t.on_his_trade(trade("buy", 913, 913_000), P_LEADER, EVM, time.time())        # cap reached
    await settle(t)
    assert abs(p.timing["bought_usd"] - 1000) < 1e-6, p.timing["bought_usd"]
    assert abs(p.timing["peak_value"] - p.cost) < 1e-6                            # each add lifted the peak
    skips = [d for (d,) in store.db.execute("SELECT detail FROM leader_events WHERE kind='xchain_paper_skip'")]
    assert any("profile trail_Leader-B: cap $1,000/token reached" in d for d in skips), skips
    # his sells (Relay + DEX) are not followed
    t.on_his_trade(trade("sell", 5000, 5_000_000), P_LEADER, EVM, time.time())
    await t.logs_once()
    rpc.logs = [{"address": TOK, "transactionHash": "0xd", "logIndex": "0x0", "blockNumber": hex(1001),
                 "topics": [TRANSFER, pad_addr(EVM), pad_addr("0x" + "99" * 20)], "data": hex(10 ** 6 * E)}]
    rpc.block = 1010
    await t.logs_once(); await settle(t)
    assert p.status == "open" and int(p.timing["remaining"]) == p.token_amount
    # marks: up to 1.4x (trail not active) -> 1.6x (active) -> drop 35% from the peak -> trailing_stop
    for price in (0.0014, 0.0016):
        relay.price_usd[TOK] = price
        await t.mark_once(); await settle(t)
        assert p.status == "open"
    relay.price_usd[TOK] = 0.0016 * 0.64
    await t.mark_once(); await settle(t)
    assert p.status == "closed" and p.exit_reason == "trailing_stop", (p.status, p.exit_reason)
    assert ("robinhood", TOK, P_LEADER) not in t.positions

    # stop -60% before activation (the global safety stop is also -60%, so check -55% does NOT stop it)
    relay.price_usd[TOK2] = 0.001
    t.on_his_trade(trade("buy", 913, 913_000, token=TOK2), P_LEADER, EVM, time.time())
    await settle(t)
    q = t.positions[("robinhood", TOK2, P_LEADER)]
    relay.price_usd[TOK2] = 0.00045
    await t.mark_once(); await settle(t)
    assert q.status == "open"
    relay.price_usd[TOK2] = 0.00039
    await t.mark_once(); await settle(t)
    assert q.status == "closed" and q.exit_reason == "stop_loss", q.exit_reason
    # default leader in the same trader: 0.4x sizing, his sell mirrored
    relay.price_usd[TOK2] = 0.001
    t.on_his_trade(trade("buy", 1000, 1_000_000, token=TOK2), OTHER, "0x" + "ab" * 20, time.time())
    await settle(t)
    d = t.positions[("robinhood", TOK2, OTHER)]
    assert abs(d.timing["bought_usd"] - 400) < 1e-6 and "profile" not in d.timing
    rpc.balance[TOK2] = 0
    t.on_his_trade(trade("sell", 1000, 1_000_000, token=TOK2), OTHER, "0x" + "ab" * 20, time.time())
    await settle(t)
    assert d.status == "closed" and d.exit_reason == "leader_sold"
    # per-position mark interval: profile 30 s, default 120 s
    assert t._mark_interval(q) == 30 and t._mark_interval(d) == 120
    # a position of his opened BEFORE the profile existed keeps the default exits (his sells mirrored)
    t2, _, relay2, rpc2 = make_trader()
    t2.profiles = {}
    relay2.price_usd[TOK] = 0.001
    t2.on_his_trade(trade("buy", 1000, 1_000_000), P_LEADER, EVM, time.time())
    await settle(t2)
    old = t2.positions[("robinhood", TOK, P_LEADER)]
    t2.profiles = profiles()                                       # profile added later (restart with new config)
    rpc2.balance[TOK] = 0
    t2.on_his_trade(trade("sell", 1000, 1_000_000), P_LEADER, EVM, time.time())
    await settle(t2)
    assert old.status == "closed" and old.exit_reason == "leader_sold" and t2._mark_interval(old) == 120
    print("xchain: 0.219x sizing, $1,000 cap incl. adds, his Relay/DEX sells ignored, trail after 1.5x, "
          "stop -60%, default leader unchanged, 30 s marks")
    return store


async def report_tests(store):
    db_path = store.db.execute("PRAGMA database_list").fetchone()[2]
    out = subprocess.run([sys.executable, "whatif_xchain.py", "--db", db_path, "--profile", "trail_Leader-B", "--list"],
                         capture_output=True, text=True, cwd=ROOT)
    assert out.returncode == 0 and "2 xchain paper positions" in out.stdout, out.stdout + out.stderr
    assert "trail_Leader-B" in out.stdout and OTHER[:8] not in out.stdout
    rep = subprocess.run([sys.executable, "-m", "scripts.xchain_report", "--db", db_path], capture_output=True,
                         text=True, cwd=ROOT)
    ours = rep.stdout.split("OUR PAPER")[1]
    profs = {l.split()[2] for l in ours.splitlines() if l[:8] in (P_LEADER[:8], OTHER[:8])}
    assert profs == {"trail_Leader-B", "default"}, ours
    print("reports: whatif_xchain --profile, xchain_report rows split by profile")


async def main():
    profile_tests()
    await solana_exit_tests()
    await solana_sizing_tests()
    store = await xchain_tests()
    await report_tests(store)
    print("ALL PASSED")


if __name__ == "__main__":
    asyncio.run(main())
