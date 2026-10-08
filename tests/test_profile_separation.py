"""Block 3 (Oct 8 2026) - profiles separate, limits, Solana adds, mirror_ladder: python -m tests.test_profile_separation

3.1 profile positions keyed by (leader, token): never block a normal leader's token and vice versa
3.2 profile positions outside the global limits; own max_open_tokens / daily_loss_usd (Solana + xchain)
3.3 Solana adds merged (weighted cost, amount), trimmed to the room left, skipped below min_usd; no re-entry lock
3.4 min_leader_buy_sol exemption for profile leaders
3.5 mirror_ladder: ladder fires once per level, ladder + mirror sells, stop on the cost of the rest after partials,
    restart keeps the state; same ladder on xchain
3.7 profile positions excluded from the H4/H5/H6 reports"""
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
from bot.xchain.paper import XchainPaperTrader
from tests.test_mirror import A, M_LEADER, MIRROR
from tests.test_profiles import FakeSol, P_LEADER
from tests.test_xchain_paper import E, EVM, GAS, TOK, TOK2, TOK3, W, FakeRelay, FakeRpc, settle, trade
from whatif_ticksv2 import load as whatif_load

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NORMAL = "NormalLeader1111111111111111111111111111111"
LADDER = {"name": "mirror_ladder_Leader-B", "mode": "mirror_ladder", "ladder_levels": [10, 25, 50, 100], "ladder_pct": 0.25,
          "stop_loss": 0.60, "target_usd": 200, "his_median_usd": 913, "max_usd_per_token": 1000, "min_usd": 25,
          "mark_interval_s": 30, "max_open_tokens": 2, "daily_loss_usd": 500}


def profiles():
    return load_profiles({"leader_profiles": {P_LEADER: LADDER, M_LEADER: MIRROR}}, paper=True)


def profile_tests():
    p = profiles()[P_LEADER]
    assert p.mode == "mirror_ladder" and p.mirrors and p.ladder_levels == (10, 25, 50, 100) and p.ladder_pct == 0.25
    e = p.exits()
    assert e["mode"] == "mirror_ladder" and e["follow_leader_exit"] and e["take_profit"] == 0 and e["trailing_pct"] == 0
    assert e["ladder_levels"] == [10, 25, 50, 100] and e["stop_basis"] == "cost" and e["stop_loss"] == 0.6
    assert p.max_open_tokens == 2 and p.daily_loss_usd == 500
    m = profiles()[M_LEADER]
    assert m.max_open_tokens == 10 and m.daily_loss_usd == 500 and m.ladder_levels == ()      # defaults
    for bad in ({**LADDER, "ladder_pct": 0}, {**LADDER, "ladder_levels": [1, 10]}, {**LADDER, "trailing_pct": 0.3},
                {**LADDER, "max_open_tokens": 0}):
        try:
            load_profiles({"leader_profiles": {P_LEADER: bad}}, paper=True)
            raise AssertionError(f"must fail: {bad}")
        except ValueError:
            pass
    print("profile: mirror_ladder validated, limits default 10 / $500")


class Sol(FakeSol):
    """FakeSol + exits: quotes at cost, sells at cost + sell_pnl."""
    def __init__(self):
        super().__init__()
        self.sell_pnl = 0.0
    async def quote_exit(self, p): return p.cost
    async def sell(self, p, slip=None): return Fill(True, None, p.token_amount, p.cost + self.sell_pnl)
    async def release(self, token): pass


def bot(profile_cfg=None, **risk):
    cfg = {"paper": True, "db_path": tempfile.mktemp(), "chains": {"solana": {
        "enabled": True, "rpc_http": "http://x", "rpc_ws": "ws://x", "leaders": [P_LEADER, M_LEADER, NORMAL],
        "buy_amount": 2.0, "max_signal_age_s": 999, "only_first_leader_buy": True, "daily_loss_limit": 1.0}},
        "risk": {"max_open_positions": 1, "reentry_cooldown_s": 3600, "kill_switch_file": tempfile.mktemp(), **risk},
        "exits": {}, "leader_profiles": profile_cfg if profile_cfg is not None else {P_LEADER: LADDER, M_LEADER: MIRROR}}
    b = Bot(cfg)
    assert b.adapters["solana"].min_buy_exempt == set(b.profiles)          # 3.4 on the real adapter
    b.adapters["solana"] = Sol()
    return b


def sig(leader, tok, sol, pre=0):
    s = Signal("solana", leader, "buy", tok, 10, sol, f"s-{tok}-{time.time()}", int(time.time()))
    s.leader_pre_amount = pre
    return s


def skips(b):
    return [r for (r,) in b.store.db.execute("SELECT reason FROM signals WHERE action='skip' ORDER BY id")]


async def separation_tests():
    b = bot()
    await b.handle_buy(sig(NORMAL, "TOKA", 1.0))                     # normal: 2 SOL
    await b.handle_buy(sig(P_LEADER, "TOKA", 4.565))                 # profile: same token -> its own position
    await b.handle_buy(sig(M_LEADER, "TOKA", 2.5))                   # second profile: a third position in TOKA
    opened = [(p.leader[:6], p.timing.get("profile")) for p in b.pm.open()]
    assert opened == [(NORMAL[:6], None), (P_LEADER[:6], "mirror_ladder_Leader-B"), (M_LEADER[:6], "mirror_Leader-A")], opened
    # max_open_positions 1 counts NORMAL positions only: a second normal token is blocked, a profile token is not
    await b.handle_buy(sig(NORMAL, "TOKB", 1.0))
    assert skips(b)[-1] == "max open positions"
    await b.handle_buy(sig(P_LEADER, "TOKB", 4.565))
    assert len(b.pm.open()) == 4
    # normal leader in a token only a profile holds: allowed (after closing his TOKA)
    pa = b.pm.open()[0]
    await b.pm.close(pa, "test")
    await b.handle_buy(sig(NORMAL, "TOKB", 1.0))
    assert sum(p.token == "TOKB" for p in b.pm.open()) == 2 and skips(b)[-1] == "max open positions"
    # re-entry: the normal leader's own close of TOKA blocks HIM for 3600 s; the profile's close of a token never locks
    await b.handle_buy(sig(NORMAL, "TOKA", 1.0))
    assert skips(b)[-1] in ("max open positions", "re-entry cooldown")
    pb = next(p for p in b.pm.open() if p.token == "TOKB" and p.timing.get("profile"))
    await b.pm.close(pb, "test")
    await b.handle_buy(sig(P_LEADER, "TOKB", 4.565))                 # re-buys right away
    assert any(p.token == "TOKB" and p.leader == P_LEADER and p.status == "open" for p in b.pm.open())
    # only_first_leader_buy is on: a normal leader's add is skipped, a profile leader's add is copied
    await b.handle_buy(sig(NORMAL, "TOKC", 1.0, pre=10**9))
    assert skips(b)[-1] == "leader already held this token (add-on buy)"
    await b.handle_buy(sig(P_LEADER, "TOKB", 4.565, pre=10**9))
    assert next(p for p in b.pm.open() if p.token == "TOKB" and p.leader == P_LEADER).timing["adds"] == 1
    # daily loss: a normal loss today does not stop a profile; the profile's own loss does (below)
    print("separation: (leader, token) positions, global limits count normal positions only, no re-entry lock, "
          "profile adds copied with only_first_leader_buy on")


async def limits_tests():
    b = bot()
    await b.handle_buy(sig(P_LEADER, "T1", 4.565))
    await b.handle_buy(sig(P_LEADER, "T2", 4.565))
    await b.handle_buy(sig(P_LEADER, "T3", 4.565))                   # max_open_tokens 2
    assert skips(b)[-1] == "profile mirror_ladder_Leader-B: max open tokens (2)"
    await b.handle_buy(sig(P_LEADER, "T1", 4.565))                   # an add is not a new token: allowed
    assert next(p for p in b.pm.open() if p.token == "T1").timing["adds"] == 1
    # daily loss: close T1 at -$600 (SOL at $200: -3 SOL) -> no new profile positions, other profiles unaffected
    p1 = next(p for p in b.pm.open() if p.token == "T1")
    b.adapters["solana"].sell_pnl = -3.0
    await b.pm.close(p1, "test")
    assert b.store.profile_pnl_today_usd("mirror_ladder_Leader-B", 200.0) < -599
    await b.handle_buy(sig(P_LEADER, "T4", 4.565))
    assert "daily loss limit $500 hit" in skips(b)[-1], skips(b)[-1]
    await b.handle_buy(sig(M_LEADER, "T4", 2.5))
    assert any(p.token == "T4" and p.leader == M_LEADER for p in b.pm.open())
    # the global daily loss limit (1 SOL) ignores the profile's -3 SOL
    assert b.store.realized_pnl_today("solana", True) == 0
    await b.handle_buy(sig(NORMAL, "T5", 1.0))
    assert any(p.token == "T5" for p in b.pm.open())
    print("limits: max_open_tokens per profile (adds exempt), daily_loss_usd per profile, globals unaffected")


async def adds_tests():
    b = bot()
    a = b.adapters["solana"]
    await b.handle_buy(sig(P_LEADER, "TOK", 4.565))                  # his $913 -> $200 = 1 SOL
    p = b.pm.open()[0]
    assert p.timing["bought_usd"] == 200 and abs(p.cost - 1.01) < 1e-9 and p.token_amount == 1000
    await b.pm._check(p)                                             # ref_value (FakeSol has no quote: skip if none)
    await b.handle_buy(sig(P_LEADER, "TOK", 15.0))                   # his $3,000 -> $657
    await b.handle_buy(sig(P_LEADER, "TOK", 15.0))                   # room $143 >= $25 -> trimmed
    await b.handle_buy(sig(P_LEADER, "TOK", 15.0))                   # room 0 -> skipped
    add1 = 200 / 913 * 3000                                          # $657.17, then the room: $142.83
    want = [1.0, add1 / 200, (800 - add1) / 200]                      # bought_usd is kept to the cent
    assert len(a.bought) == 3 and all(abs(x - y) < 1e-4 for x, y in zip(a.bought, want)), a.bought
    assert p.timing["adds"] == 2 and len(p.timing["add_ts"]) == 2 and p.timing["bought_usd"] == 1000
    assert p.token_amount == 3000 and abs(p.cost - (1.03 + 800 / 200)) < 1e-4, p.cost
    assert skips(b)[-1] == "profile mirror_ladder_Leader-B: cap $1,000/token reached"
    rows = b.store.db.execute("SELECT action, reason FROM signals WHERE token='TOK' ORDER BY id").fetchall()
    assert [r[0] for r in rows] == ["bought", "bought", "bought", "skip"] and rows[1][1].startswith("add #1")
    saved = b.store.load_open(True)
    assert len(saved) == 1 and saved[0].token_amount == 3000 and saved[0].timing["adds"] == 2
    # a small add below min_usd is skipped even with room
    await b.handle_buy(sig(M_LEADER, "TOKM", 2.5))                   # $500 -> $200
    await b.handle_buy(sig(M_LEADER, "TOKM", 0.25))                  # $50 -> $20 < $25
    assert skips(b)[-1] == "profile mirror_Leader-A: our size $20.00 < $25"
    print("adds: merged (amount, cost, bought_usd, add_ts), trimmed to the room, cap / min skips, saved")


def pm_with(path, timing, cost=1.0, tokens=1000):
    store = Store(tempfile.mktemp())
    glob = {"take_profit": 0.3, "stop_loss": 0.2, "max_hold_s": 0, "stop_basis": "entry_price",
            "trailing_activate": 0, "trailing_pct": 0, "mark_interval_s": 0, "follow_leader_exit": False}
    pm = PositionManager(glob, {"solana": A(path)}, store)
    p = Position(None, "solana", "T", P_LEADER, tokens, cost, time.time(), None, True, timing=dict(timing))
    p.id = store.open_position(p); pm.add(p)
    return p, pm


async def ladder_tests():
    stamp = profiles()[P_LEADER].stamp()
    # 1000 tokens for 1.0 SOL = 0.001 per token. 10.5x -> ladder 10x: sell 25% of 1000; same price again: nothing;
    # 30x -> 25x: 25% of 750; 200x -> 50x and 100x in one check: 25% of 563, then 25% of 423
    p, pm = pm_with([0.001, 0.0105, 0.0105, 0.03, 0.2, 0.2], stamp)
    a = pm.adapters["solana"]
    for _ in range(6):
        await pm._check(p)
    assert a.sold == [250, 187, 140, 105], a.sold
    assert p.timing["ladder_fired"] == [10, 25, 50, 100] and p.token_amount == 318 and p.status == "open"
    assert p.timing["partial_sells"] == 4 and p.timing["initial_amount"] == 1000
    realized = 250 * 0.0105 + (187 + 140 + 105) * 0.2          # A() sells at the NEXT path price after the quote
    assert abs(p.timing["realized"] - realized) < 1e-9, p.timing["realized"]
    # levels never fire twice, even far above: nothing more is sold at 500x
    a.path = [0.5]; a.i = 0
    await pm._check(p)
    assert a.sold == [250, 187, 140, 105]
    # ladder + mirror: he sells 40% -> we sell 40% of what we hold (318 -> 191); then 10x again: already fired
    await pm.on_leader_sell("solana", P_LEADER, "T", 0.4)
    assert a.sold[-1] == 127 and p.token_amount == 191
    # stop on the COST of what we still hold (not the entry quote): rest = 191/1000 of 1.0 SOL cost = 0.191;
    # at 0.0005/token the rest is worth 0.0955 = -50%: holds; at 0.00038 = -60.2%: stop
    a.path = [0.0005, 0.00038]; a.i = 0
    await pm._check(p)
    assert p.status == "open"
    await pm._check(p)
    assert p.status == "closed" and p.exit_reason == "stop_loss", (p.status, p.exit_reason)
    assert abs(p.exit_value - (p.timing["realized"] - 0 + 191 * 0.00038)) < 1e-9 or p.exit_value > realized
    # restart: ladder state, partials and exits come back; an add raises the average entry (levels move up)
    p, pm = pm_with([0.001, 0.0105], stamp)
    await pm._check(p); await pm._check(p)
    assert p.timing["ladder_fired"] == [10]
    r = pm.store.load_open(True)[0]
    assert r.timing["ladder_fired"] == [10] and r.timing["exits"]["mode"] == "mirror_ladder" and r.token_amount == 750
    pm2 = PositionManager({"take_profit": 0.3, "stop_loss": 0.2, "max_hold_s": 0, "mark_interval_s": 0},
                          {"solana": A([0.0105, 0.012])}, pm.store)
    pm2.add(r)
    await pm2._check(r); await pm2._check(r)
    assert pm2.adapters["solana"].sold == [] and r.status == "open"                  # 10x already done
    # mirror-only profile: his sell mirrored, no ladder
    q, pmq = pm_with([0.001, 0.05], profiles()[M_LEADER].stamp())
    q.leader = M_LEADER
    await pmq._check(q); await pmq._check(q)
    assert pmq.adapters["solana"].sold == [] and "ladder_fired" not in q.timing
    print("ladder: once per level (several per check), + mirror sells, stop on the cost of the rest, restart state")


def make_trader(**cfg):
    store = Store(tempfile.mktemp())
    base = {"chains": ["robinhood", "bsc"], "entry_delay_s": 0, "dex_confirm_s": 0, "sell_retries": 1,
            "max_open_positions": 1, "max_open_per_leader": 1, "safety_stop": 0.30, **cfg}
    t = XchainPaperTrader(base, store, True, [P_LEADER, M_LEADER, NORMAL], kill_switch_file=tempfile.mktemp(),
                          profiles=profiles(), global_exits={"take_profit": 0.3, "stop_loss": 0.2})
    relay, rpc = FakeRelay(), FakeRpc(evms=(EVM,))
    t.client, t.rpc, t._eth = relay, {"robinhood": "rpc://rh"}, rpc
    return t, store, relay, rpc


async def xchain_tests():
    t, store, relay, rpc = make_trader()
    for tok in (TOK, TOK2, TOK3):
        relay.price_usd[tok] = 0.001
    t.on_his_trade(trade("buy", 913, 913_000), P_LEADER, EVM, time.time())           # profile: $200
    t.on_his_trade(trade("buy", 1000, 1_000_000, token=TOK2), NORMAL, EVM, time.time())   # normal: allowed (1 open)
    await settle(t)
    assert set(t.positions) == {("robinhood", TOK, P_LEADER), ("robinhood", TOK2, NORMAL)}
    t.on_his_trade(trade("buy", 1000, 1_000_000, token=TOK3), NORMAL, EVM, time.time())   # 2nd normal: global limit
    t.on_his_trade(trade("buy", 913, 913_000, token=TOK3), P_LEADER, EVM, time.time())  # profile: 2nd token ok
    t.on_his_trade(trade("buy", 913, 913_000, token=TOK2), P_LEADER, EVM, time.time())  # 3rd token: max_open_tokens 2
    await settle(t)
    det = [d for (d,) in store.db.execute("SELECT detail FROM leader_events WHERE kind='xchain_paper_skip' ORDER BY rowid")]
    assert any("max open xchain positions (1)" in d for d in det) and any("max open tokens (2)" in d for d in det), det
    assert len(t.positions) == 3
    p = t.positions[("robinhood", TOK, P_LEADER)]
    # ladder: 12x -> sell 25% once; 12x again: nothing; his sell 50% -> mirrored on the rest; -61% -> stop
    relay.price_usd[TOK] = 0.012
    await t.mark_once(); await settle(t)
    rem0 = int(p.timing["remaining"])
    assert p.timing["ladder_fired"] == [10] and p.status == "open"
    f = store.db.execute("SELECT reason, fraction FROM xchain_fills WHERE side='sell' ORDER BY id DESC LIMIT 1").fetchone()
    assert f == ("partial ladder_10x", 0.25), f
    await t.mark_once(); await settle(t)
    assert int(p.timing["remaining"]) == rem0
    rpc.balance[TOK] = 456_500 * E
    t.on_his_trade(trade("sell", 5000, 456_500), P_LEADER, EVM, time.time())
    await settle(t)
    assert abs(int(p.timing["remaining"]) / rem0 - 0.5) < 1e-6 and p.status == "open"
    relay.price_usd[TOK] = 0.001 * 0.39
    await t.mark_once(); await settle(t)
    assert p.status == "closed" and p.exit_reason == "safety_stop", (p.status, p.exit_reason)
    # restart: ladder state persists for the open profile position
    q = t.positions[("robinhood", TOK3, P_LEADER)]
    relay.price_usd[TOK3] = 0.03
    await t.mark_once(); await settle(t)
    assert q.timing["ladder_fired"] == [10, 25]
    t2 = XchainPaperTrader({"chains": ["robinhood"]}, store, True, [P_LEADER], profiles=profiles())
    assert t2.positions[("robinhood", TOK3, P_LEADER)].timing["ladder_fired"] == [10, 25]
    print("xchain: profile outside the 1 open / 1 per leader limits, own max_open_tokens, ladder once per level, "
          "mirror + stop on the rest, state persists")
    return store


def report_tests(xstore):
    # H4/H5: whatif_ticksv2 drops profile positions by default; H6: whatif_xchain --profile H6 drops them
    d = tempfile.mkdtemp()
    path = os.path.join(d, "t.db")
    st = Store(path)
    for pid, leader, timing in ((1, NORMAL, {"fill_model": "pumpswap_amm+0.8s"}),
                                (2, P_LEADER, {"fill_model": "pumpswap_amm+0.8s", "profile": "mirror_ladder_Leader-B",
                                               "exits": {"mode": "mirror_ladder"}})):
        st.db.execute("INSERT INTO positions(id,chain,token,leader,token_amount,cost,opened_at,paper,status,exit_value,"
                      "closed_at,exit_reason,timing) VALUES(?,'solana',?,?,'1000',2.0,?,1,'closed',1.6,?,'stop_loss',?)",
                      (pid, f"TOK{pid}", leader, 1e9 + pid, 1e9 + pid + 60, json.dumps(timing)))
        for dt in (0, 10):
            st.db.execute("INSERT INTO ticks VALUES(?,?,?,?,?,?,?,?)", (1e9 + pid + dt, 1, f"TOK{pid}", "pumpswap_amm", 10**12, 10**11, 0, 0))
    st.db.commit()
    data, _, _, excluded, _ = whatif_load(path, 1, {"pumpswap_amm": 100}, 0.01, 3600, exclude_profiles=True)
    assert [p.pid for p in data] == [1] and excluded == [(2, P_LEADER, "mirror_ladder_Leader-B")]
    xdb = xstore.db.execute("PRAGMA database_list").fetchone()[2]
    out = subprocess.run([sys.executable, "whatif_xchain.py", "--db", xdb, "--profile", "H6", "--list"],
                         capture_output=True, text=True, cwd=ROOT)
    assert out.returncode == 0 and P_LEADER[:8] not in out.stdout and NORMAL[:8] in out.stdout, out.stdout + out.stderr
    print("reports: profile positions excluded from whatif_ticksv2 (H4/H5) and whatif_xchain --profile H6")


def main():
    profile_tests()
    asyncio.run(separation_tests())
    asyncio.run(limits_tests())
    asyncio.run(adds_tests())
    asyncio.run(ladder_tests())
    xstore = asyncio.run(xchain_tests())
    report_tests(xstore)
    print("ALL PASSED")


if __name__ == "__main__":
    main()
