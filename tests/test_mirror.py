"""Mirror profile (paper): python -m tests.test_mirror

mode "mirror" (bot/profiles.py): his buys copied with proportional sizing, his sells copied pro rata on Solana
(fraction from his pre-balance -> PositionManager.partial_sell) and on xchain (Relay / DEX sells), no TP, no
trailing, safety stop -60% on what we still hold. Default leaders and trail profiles unchanged."""
import asyncio
import json
import tempfile
import time

from bot.main import Bot
from bot.models import Fill, Position, Signal
from bot.positions import PositionManager
from bot.profiles import load_profiles
from bot.store import Store
from bot.xchain.paper import XchainPaperTrader
from tests.test_profiles import P_LEADER, PROFILE, FakeSol
from tests.test_xchain_paper import E, EVM, GAS, TOK, TOK2, FakeRelay, FakeRpc, settle, trade

M_LEADER = "Leader-A"
MIRROR = {"name": "mirror_Leader-A", "mode": "mirror", "stop_loss": 0.60, "target_usd": 200, "his_median_usd": 500,
          "max_usd_per_token": 500, "min_usd": 25, "mark_interval_s": 120}


def profiles():
    return load_profiles({"leader_profiles": {P_LEADER: PROFILE, M_LEADER: MIRROR}}, paper=True)


def profile_tests():
    m = profiles()[M_LEADER]
    assert m.mirrors and m.factor == 0.4 and m.trailing_pct == 0 and m.trailing_activate == 0
    e = m.exits()
    assert e == {"mode": "mirror", "take_profit": 0.0, "trailing_activate": 0.0, "trailing_pct": 0.0,
                 "stop_loss": 0.6, "max_hold_s": 0.0, "follow_leader_exit": True}, e
    assert not profiles()[P_LEADER].mirrors and not profiles()[P_LEADER].exits()["follow_leader_exit"]
    assert m.size_usd(500) == (200.0, None) and m.size_usd(50)[0] == 0           # $20 < $25
    assert m.size_usd(2000, already_usd=300) == (200.0, None)                      # cap $500 incl. adds
    for bad in ({**MIRROR, "trailing_pct": 0.3}, {**MIRROR, "stop_loss": 0}):
        try:
            load_profiles({"leader_profiles": {M_LEADER: bad}}, paper=True)
            raise AssertionError(f"must fail: {bad}")
        except ValueError:
            pass
    assert load_profiles({"leader_profiles": {M_LEADER: MIRROR}}, paper=False) == {}
    print("profile: mirror = follow his sells, factor 0.4, cap $500, min $25, no trailing allowed, paper-only")


class A:
    """Solana adapter stand-in: a price path per call; sells are valued at the current price x tokens."""
    def __init__(self, path, fail_first=0):
        self.path, self.i, self.sold, self.fail = path, 0, [], fail_first
    def price(self):
        return self.path[min(self.i, len(self.path) - 1)]
    async def quote_exit(self, p):
        v = self.price() * p.token_amount; self.i += 1; return v
    async def sell(self, p, slip=None):
        if self.fail:
            self.fail -= 1
            return Fill(False, None, 0, 0.0, "rate limit")
        self.sold.append(p.token_amount)
        return Fill(True, None, p.token_amount, self.price() * p.token_amount)


def pm_with(path, timing, cost=1.0, tokens=1000, fail_first=0):
    store = Store(tempfile.mktemp())
    glob = {"take_profit": 0.3, "stop_loss": 0.2, "max_hold_s": 0, "stop_basis": "entry_price",
            "trailing_activate": 0, "trailing_pct": 0, "mark_interval_s": 0, "follow_leader_exit": False}
    pm = PositionManager(glob, {"solana": A(path, fail_first)}, store)
    p = Position(None, "solana", "T", M_LEADER, tokens, cost, time.time(), None, True, timing=dict(timing))
    p.id = store.open_position(p); pm.add(p)
    return p, pm


async def solana_tests():
    stamp = profiles()[M_LEADER].stamp()
    # 1000 tokens for 1.0 SOL; price 0.001/token. He sells 30% -> we sell 300 tokens at 0.0012 = 0.36
    p, pm = pm_with([0.001, 0.0012, 0.0012, 0.0008, 0.0008, 0.0008], stamp)
    await pm._check(p)                                                   # ref_value = 1.0
    await pm.on_leader_sell("solana", M_LEADER, "T", 0.3)
    a = pm.adapters["solana"]
    assert a.sold == [300] and p.status == "open" and p.token_amount == 700
    assert abs(p.timing["realized"] - 0.36) < 1e-9 and p.timing["initial_amount"] == 1000 and p.timing["partial_sells"] == 1
    row = pm.store.db.execute("SELECT token_amount, cost, timing FROM positions WHERE id=?", (p.id,)).fetchone()
    assert row[0] == "700" and row[1] == 1.0 and json.loads(row[2])["realized"] == p.timing["realized"]
    # mark = rest + realized (700 x 0.0012 + 0.36 = 1.2); no TP although +20%... (TP is 0 for the profile)
    await pm._check(p)
    assert p.status == "open" and abs(pm.store.db.execute("SELECT MAX(value) FROM marks").fetchone()[0] - 1.2) < 1e-9
    # a -20% dip of the REST (0.0008: rest 0.56 + 0.36 = 0.92) is not a stop for the profile (-60%)
    await pm._check(p)
    assert p.status == "open"
    # he sells 50% of what he has left -> we sell 350 at 0.0008 = 0.28; then everything -> closed
    await pm.on_leader_sell("solana", M_LEADER, "T", 0.5)
    assert a.sold == [300, 350] and p.token_amount == 350 and abs(p.timing["realized"] - 0.64) < 1e-9
    await pm.on_leader_sell("solana", M_LEADER, "T", 1.0)
    assert p.status == "closed" and p.exit_reason == "leader_sold" and abs(p.exit_value - 0.92) < 1e-9, p.exit_value
    # safety stop on the rest: after a 50% partial at 1.0, the rest collapses to 0.00039 x 500 = 0.195 (-61%)
    p, pm = pm_with([0.001, 0.001, 0.00045, 0.00039], stamp)
    await pm._check(p)                                                   # ref 1.0 (price index -> 1)
    await pm.on_leader_sell("solana", M_LEADER, "T", 0.5)                # 500 sold at 0.001 = 0.5 realized
    await pm._check(p)                                                   # rest 0.5: flat
    await pm._check(p)                                                   # -55% on the rest: holds
    assert p.status == "open"
    await pm._check(p)                                                   # -61%: stop
    assert p.status == "closed" and p.exit_reason == "stop_loss" and abs(p.exit_value - (0.5 + 0.195)) < 1e-9
    # a sell that fails twice (rate limit) is retried, not lost
    p, pm = pm_with([0.001], stamp, fail_first=2)
    await pm.on_leader_sell("solana", M_LEADER, "T", 0.25)
    assert p.token_amount == 750 and pm.adapters["solana"].sold == [250]
    # ref_value set only after a partial (first quote failed until then): still the whole position
    p, pm = pm_with([0.001, 0.001], stamp)
    await pm.on_leader_sell("solana", M_LEADER, "T", 0.5)
    await pm._check(p)
    assert abs(p.ref_value - 1.0) < 1e-9
    # default leader (global follow_leader_exit false): his sell ignored; trail profile: ignored too
    p, pm = pm_with([0.001], {})
    await pm.on_leader_sell("solana", M_LEADER, "T", 0.5)
    assert p.status == "open" and p.token_amount == 1000
    p, pm = pm_with([0.001], profiles()[P_LEADER].stamp())
    p.leader = P_LEADER
    await pm.on_leader_sell("solana", P_LEADER, "T", 0.5)
    assert p.status == "open" and p.token_amount == 1000
    # a position that follows the leader without a mirror profile closes entirely on any sell
    p, pm = pm_with([0.001], {})
    pm.follow_exit = True
    await pm.on_leader_sell("solana", M_LEADER, "T", 0.3)
    assert p.status == "closed" and p.exit_value == 1.0
    # restart: partial state comes back from the DB
    p, pm = pm_with([0.001, 0.001], stamp)
    await pm.on_leader_sell("solana", M_LEADER, "T", 0.4)
    r = pm.store.load_open(True)[0]
    assert r.token_amount == 600 and r.timing["realized"] == 0.4 and r.timing["exits"]["mode"] == "mirror"
    print("solana: pro-rata partial sells, realized + rest in marks/PnL, -60% stop on the rest, retries, "
          "default + trail leaders unchanged, survives restart")


async def bot_fraction_tests():
    """Bot.consume: the fraction comes from his sell tx (sold / pre-balance); unknown pre-balance = all."""
    cfg = {"paper": True, "db_path": tempfile.mktemp(), "chains": {"solana": {
        "enabled": True, "rpc_http": "http://x", "rpc_ws": "ws://x", "leaders": [M_LEADER],
        "buy_amount": 2.0, "max_signal_age_s": 999}}, "risk": {"max_open_positions": 10, "reentry_cooldown_s": 0},
        "exits": {"follow_leader_exit": False}, "leader_profiles": {M_LEADER: MIRROR}}
    b = Bot(cfg)
    b.adapters["solana"] = FakeSol()
    await b.handle_buy(Signal("solana", M_LEADER, "buy", "TOKA", 10, 2.5, "s1", int(time.time())))   # $500 -> $200 = 1 SOL
    p = b.pm.open()[0]
    assert p.timing["profile"] == "mirror_Leader-A" and p.timing["exits"]["follow_leader_exit"] and p.cost == 1.01
    seen = []
    async def on_sell(chain, leader, token, fraction=1.0): seen.append(round(fraction, 4))
    b.pm.on_leader_sell = on_sell
    for amount, pre in ((250, 1000), (1000, 1000), (1200, 1000), (5, 0)):
        s = Signal("solana", M_LEADER, "sell", "TOKA", amount, 0.1, f"x{amount}", int(time.time()))
        s.leader_pre_amount = pre
        await b.queue.put(s)
    task = asyncio.create_task(b.consume())
    while len(seen) < 4:
        await asyncio.sleep(0.01)
    task.cancel()
    assert seen == [0.25, 1.0, 1.0, 1.0], seen
    print("bot: fraction = sold / his pre-balance, capped at 1, unknown pre-balance -> all")


def make_trader(**cfg):
    store = Store(tempfile.mktemp())
    base = {"chains": ["robinhood", "bsc"], "entry_delay_s": 0, "dex_confirm_s": 0, "sell_retries": 1,
            "safety_stop": 0.30, **cfg}                                     # global stop tighter than the profile
    t = XchainPaperTrader(base, store, True, [P_LEADER, M_LEADER], kill_switch_file=tempfile.mktemp(),
                          profiles=profiles())
    relay, rpc = FakeRelay(), FakeRpc(evms=(EVM,))
    t.client, t.rpc, t._eth = relay, {"robinhood": "rpc://rh"}, rpc
    return t, store, relay, rpc


async def xchain_tests():
    t, store, relay, rpc = make_trader()
    relay.price_usd[TOK] = 0.001
    t.on_his_trade(trade("buy", 500, 500_000), M_LEADER, EVM, time.time())          # -> $200
    await settle(t)
    p = t.positions[("robinhood", TOK, M_LEADER)]
    assert abs(p.timing["bought_usd"] - 200) < 1e-6 and p.timing["profile"] == "mirror_Leader-A"
    assert p.timing["exits"]["mode"] == "mirror" and "peak_value" not in p.timing
    # adds up to the $500 cap incl. his adds
    t.on_his_trade(trade("buy", 1000, 1_000_000), M_LEADER, EVM, time.time())       # $400 -> only $300 left
    t.on_his_trade(trade("buy", 500, 500_000), M_LEADER, EVM, time.time())          # cap reached
    await settle(t)
    assert abs(p.timing["bought_usd"] - 500) < 1e-6
    skips = [d for (d,) in store.db.execute("SELECT detail FROM leader_events WHERE kind='xchain_paper_skip'")]
    assert any("profile mirror_Leader-A: cap $500/token reached" in d for d in skips), skips
    # his Relay sell of 25% (balanceOf: 1.5M left of 2M) -> we sell 25% of ours
    rem0 = int(p.timing["remaining"])
    rpc.balance[TOK] = 1_500_000 * E
    t.on_his_trade(trade("sell", 500, 500_000), M_LEADER, EVM, time.time())
    await settle(t)
    assert p.status == "open" and abs(int(p.timing["remaining"]) / rem0 - 0.75) < 1e-6
    f = store.db.execute("SELECT side, reason, fraction FROM xchain_fills ORDER BY id DESC LIMIT 1").fetchone()
    assert f == ("sell", "partial leader_sold", 0.25), f
    # -50% on the rest: the global 30% safety stop must NOT fire (profile stop is -60%); -61% does
    relay.price_usd[TOK] = 0.0005
    await t.mark_once(); await settle(t)
    assert p.status == "open"
    relay.price_usd[TOK] = 0.00039
    await t.mark_once(); await settle(t)
    assert p.status == "closed" and p.exit_reason == "safety_stop", (p.status, p.exit_reason)
    # DEX sell of everything -> mirrored (trail profile would ignore it)
    relay.price_usd[TOK2] = 0.001
    t.on_his_trade(trade("buy", 500, 500_000, token=TOK2), M_LEADER, EVM, time.time())
    await settle(t)
    q = t.positions[("robinhood", TOK2, M_LEADER)]
    await t.logs_once()
    rpc.balance[TOK2] = 0
    rpc.logs = [{"address": TOK2, "transactionHash": "0xd", "logIndex": "0x0", "blockNumber": hex(1001),
                 "topics": ["0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef",
                            "0x" + "0" * 24 + EVM[2:].lower(), "0x" + "0" * 24 + "99" * 20], "data": hex(500_000 * E)}]
    rpc.block = 1010
    await t.logs_once(); await settle(t)
    assert q.status == "closed" and q.exit_reason == "leader_sold_dex", (q.status, q.exit_reason)
    print("xchain: 0.4x sizing, $500 cap incl. adds, Relay + DEX sells mirrored pro rata, profile stop -60% "
          "replaces the global safety stop")


def main():
    profile_tests()
    asyncio.run(solana_tests())
    asyncio.run(bot_fraction_tests())
    asyncio.run(xchain_tests())
    print("ALL PASSED")


if __name__ == "__main__":
    main()
