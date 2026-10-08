"""Block 4 (Oct 8 2026) - leader status + leader selection: python -m tests.test_leader_status

4.1 leader_status watch: signals recorded + price path observed, no Solana / xchain positions, never blocks a token
4.2 scripts/leader_selection: CUT / PROMOTE / WATCH rules, flagged + profile positions excluded, watch-leader gap
    from ticks, last-trade from signals."""
import asyncio
import json
import os
import tempfile
import time

from bot.main import Bot
from bot.models import Fill, Signal
from bot.store import Store
from bot.xchain.paper import XchainPaperTrader
from scripts.leader_selection import collect, open_db
from tests.test_profiles import FakeSol
from tests.test_xchain_paper import EVM, TOK, FakeRelay, FakeRpc, settle, trade

WATCH = "Leader-C"
ACTIVE = "ActiveLeader111111111111111111111111111111"


class Sol(FakeSol):
    def __init__(self):
        super().__init__()
        self.observed = []
    async def observe(self, token): self.observed.append(token)


def cfg(status):
    return {"paper": True, "db_path": tempfile.mktemp(), "chains": {"solana": {
        "enabled": True, "rpc_http": "http://x", "rpc_ws": "ws://x", "leaders": [WATCH, ACTIVE], "buy_amount": 2.0,
        "max_signal_age_s": 999, "leader_status": {WATCH: status}}},
        "risk": {"max_open_positions": 5, "reentry_cooldown_s": 0, "kill_switch_file": tempfile.mktemp()}, "exits": {},
        "xchain": {"enabled": True, "paper_trading": {"enabled": True}}}


async def status_tests():
    b = Bot(cfg("watch"))
    assert b.watch == {WATCH} and b.xchain.trader.watch == {WATCH}
    a = Sol()
    b.adapters["solana"] = a
    sig = lambda leader, tok: Signal("solana", leader, "buy", tok, 10, 1.0, f"s-{tok}-{leader[:3]}", int(time.time()))
    await b.handle_buy(sig(WATCH, "TOKA"))
    await asyncio.sleep(0.01)                                      # observe() is scheduled, not awaited
    assert b.pm.open() == [] and a.observed == ["TOKA"] and a.bought == []
    rows = b.store.db.execute("SELECT leader, action, reason FROM signals").fetchall()
    assert rows == [(WATCH, "skip", "watch leader")], rows
    await b.handle_buy(sig(ACTIVE, "TOKA"))                        # the watch leader never blocks a token
    assert [p.leader for p in b.pm.open()] == [ACTIVE]
    assert Bot(cfg("active")).watch == set()
    try:
        Bot(cfg("paused"))
        raise AssertionError("unknown status must fail")
    except ValueError:
        pass
    # xchain: his Relay buy is skipped and recorded
    store = Store(tempfile.mktemp())
    t = XchainPaperTrader({"entry_delay_s": 0}, store, True, [WATCH, ACTIVE], kill_switch_file=tempfile.mktemp(),
                          watch={WATCH})
    relay = FakeRelay()
    relay.price_usd[TOK] = 0.001
    t.client = relay
    t.on_his_trade(trade("buy", 1000, 1_000_000), WATCH, EVM, time.time())
    t.on_his_trade(trade("buy", 1000, 1_000_000), ACTIVE, EVM, time.time())
    await settle(t)
    assert list(t.positions) == [("robinhood", TOK, ACTIVE)]
    ev = store.db.execute("SELECT leader, detail FROM leader_events WHERE kind='xchain_paper_skip'").fetchall()
    assert ev == [(WATCH, "robinhood TKN his $1,000.00: watch leader")], ev
    print("status watch: Solana + xchain buys skipped and recorded, price path observed, token not blocked")


T0 = time.time() - 2 * 86400


def seed(path):
    st = Store(path)
    db = st.db
    pid = [0]

    def pos(leader, cost, exitv, gap_pct, opened, chain="solana", model="pumpswap_amm+0.8s", timing=None, status="closed"):
        pid[0] += 1
        tokens = 1000
        his_price = cost / tokens / (1 + gap_pct / 100)                  # ours (cost incl. fee) / his - 1 = gap
        tok = f"TOK{pid[0]}"
        db.execute("INSERT INTO signals(ts,chain,leader,side,token,token_amount,quote_amount,action) VALUES(?,?,?,'buy',?,?,?,'bought')",
                   (opened - 1, chain, leader, tok, str(tokens), his_price * tokens))
        db.execute("INSERT INTO positions(id,chain,token,leader,token_amount,cost,opened_at,paper,status,exit_value,closed_at,"
                   "exit_reason,timing) VALUES(?,?,?,?,?,?,?,1,?,?,?,'x',?)",
                   (pid[0], chain, tok, leader, str(tokens), cost, opened, status, exitv, opened + 60,
                    json.dumps({"fill_model": model, **(timing or {})})))
        if chain != "solana":
            db.execute("INSERT INTO xchain_fills(ts,position_id,chain,token,side,reason,gap_pct) VALUES(?,?,?,?,'buy','open',?)",
                       (opened, pid[0], chain, tok, gap_pct))
        return pid[0]

    L = {k: f"{k}_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx" for k in ("GAP", "LOSS", "OLD", "PROMO", "WATCH", "NEW", "PROF")}
    for i in range(5):                                            # CUT by gap: 5 entries at +30%
        pos(L["GAP"], 2.01, 2.5, 30, T0 + i * 100)
    for i in range(15):                                           # CUT by losses: 15 closed, avg -10%, one winner
        pos(L["LOSS"], 2.01, 2.5 if i == 0 else 1.8, 3, T0 + i * 100)
    pos(L["OLD"], 2.01, 2.5, 1, T0 - 20 * 86400)                  # CUT: last signal 20 days ago
    for i in range(30):                                           # PROMOTE: 30 closed, small steady wins, gap +2%
        pos(L["PROMO"], 2.01, 2.2 if i % 3 else 1.9, 2, T0 + i * 100)
    pos(L["PROMO"], 200.0, 250.0, 2, T0 + 5000, chain="robinhood") # + one xchain win (USD)
    for i in range(3):                                            # WATCH: too few
        pos(L["NEW"], 2.01, 2.2, 1, T0 + i * 100, model="jupiter+0.8s")
    pos(L["NEW"], 2.01, 1.5, 1, T0 + 400)                         # pool loss
    flagged = pos(L["NEW"], 2.01, 0.2, 1, T0 + 500)              # flagged: -90% in one mark step
    db.execute("INSERT INTO marks VALUES(?,?,2.0)", (flagged, T0 + 501)); db.execute("INSERT INTO marks VALUES(?,?,0.2)", (flagged, T0 + 502))
    pos(L["NEW"], 2.01, 9.0, 1, T0 + 600, timing={"profile": "mirror_Leader-A"})   # profile position: not counted
    # watch leader: skipped signals + ticks 5 s later at +25% / +10% vs his price
    for i, gap in enumerate((25, 10)):
        tok, his = f"WTOK{i}", 1e-6
        db.execute("INSERT INTO signals(ts,chain,leader,side,token,token_amount,quote_amount,action,reason) "
                   "VALUES(?,'solana',?,'buy',?,?,?,'skip','watch leader')", (T0 + 7000 + i, L["WATCH"], tok, "1000", his * 1000))
        # his price 1e-6 SOL x 1000 raw -> 1000 lamports per raw; the tick's sol_res/tok_res = 1000 x (1 + gap)
        db.execute("INSERT INTO ticks VALUES(?,?,?,?,?,?,?,?)",
                   (T0 + 7005 + i, 1, tok, "pumpswap_amm", 10**9, int(1000 * (1 + gap / 100) * 10**9), 0, 0))
    db.commit()
    db.close()
    return L


def selection_tests():
    path = os.path.join(tempfile.mkdtemp(), "t.db")
    L = seed(path)
    db = open_db(path)
    from scripts.flags import flagged_positions
    fl = flagged_positions(db)
    assert len(fl) == 1
    st = collect(db, sol_usd=100.0, flagged=fl, now=T0 + 10000)
    g = st[L["GAP"]]
    assert g["label"] == "CUT" and g["rule"].startswith("CUT: median gap +30% over 5 entries"), g
    lo = st[L["LOSS"]]
    assert lo["label"] == "CUT" and "15 closed" in lo["rule"] and lo["wo_best"] < 0 and lo["avg"] < -5, lo
    assert lo["wins"] == 1 and abs(lo["pnl_sol"] - (0.49 - 14 * 0.21)) < 1e-9
    old = st[L["OLD"]]
    assert old["label"] == "CUT" and "no buy signal in 14 days" in old["rule"], old
    pr = st[L["PROMO"]]
    assert pr["label"] == "PROMOTE" and pr["closed"] == 31 and pr["pnl_usd"] == 50.0 and abs(pr["gap"] - 2.0) < 1e-9, pr
    assert abs(pr["wo_best2"] - (pr["pnl_sol"] * 100 + 50 - 50 - 19)) < 1e-6, pr
    new = st[L["NEW"]]
    assert new["label"] == "WATCH" and new["closed"] == 4 and new["entries"] == 4, new   # flagged + profile left out
    assert abs(new["jup"] - 3 * 0.19) < 1e-9 and abs(new["pool"] - (-0.51)) < 1e-9 and new["profile"] == "mirror_Leader-A"
    w = st[L["WATCH"]]
    assert w["closed"] == 0 and w["entries"] == 2 and abs(w["gap"] - 17.5) < 1e-6 and w["label"] == "WATCH", w
    assert abs(w["gap_hi"] - 50.0) < 1e-9
    # --include-flagged: the -90% trade counts
    st2 = collect(db, sol_usd=100.0, flagged=None, now=T0 + 10000)
    assert st2[L["NEW"]]["closed"] == 5
    print("leader_selection: CUT (gap / losses / inactive), PROMOTE, WATCH, pool/jup split, watch-leader gap from ticks, "
          "flagged + profile positions excluded")


def main():
    asyncio.run(status_tests())
    selection_tests()
    print("ALL PASSED")


if __name__ == "__main__":
    main()
