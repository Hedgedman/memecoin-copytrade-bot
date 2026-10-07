"""Leader scorecard: python -m tests.test_leader_report

Builds a small copybot.db (real schema via Store) and checks every column of scripts/leader_report.collect:
PnL, w/o best, wins, peak from ticks (exit-independent) vs marks, entry gap, venue mix, open count, filters."""
import json
import os
import tempfile

from bot.store import Store
from scripts.leader_report import collect, open_db

L1, L2 = "LEADER_ONE_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx", "LEADER_TWO_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
T0 = 1_700_000_000.0                                   # Nov 2023: safely older than any --hours window


def seed(path):
    st = Store(path)
    db = st.db

    def pos(pid, chain, token, leader, tokens, cost, opened, exit_value, closed, timing, status="closed"):
        db.execute("INSERT INTO positions(id,chain,token,leader,token_amount,cost,opened_at,paper,status,exit_value,"
                   "closed_at,exit_reason,timing) VALUES(?,?,?,?,?,?,?,1,?,?,?,?,?)",
                   (pid, chain, token, leader, str(tokens), cost, opened, status, exit_value, closed, "x",
                    json.dumps(timing)))

    def sig(leader, token, ts, tokens, sol):
        db.execute("INSERT INTO signals(ts,chain,leader,side,token,token_amount,quote_amount,action) "
                   "VALUES(?,'solana',?,'buy',?,?,?,'bought')", (ts, leader, token, str(tokens), sol))

    def tick(token, ts, price):               # sol_res / tok_res = price
        db.execute("INSERT INTO ticks VALUES(?,?,?,?,?,?,?,?)", (ts, 1, token, "pumpswap_amm", 10**9, int(price * 10**9), 0, 0))

    # L1 solana #1: leader paid 1.0 SOL for 1000 tokens (price 0.001); we paid 2.0 for 1900 (+5.3% gap),
    # stopped at 1.6 (-20%), but the token ran to 2.5x within the 6 h window, and dumped after it.
    sig(L1, "TOK_A", T0, 1000, 1.0)
    pos(1, "solana", "TOK_A", L1, 1900, 2.0, T0 + 1, 1.6, T0 + 30, {"filled_at": T0 + 1, "fill_model": "pumpswap_amm+0.8s"})
    for dt, pr in [(0, 1.0), (2, 0.9), (20, 0.8), (100, 2.5), (5 * 3600, 2.0), (7 * 3600, 9.0)]:
        tick("TOK_A", T0 + 1 + dt, pr)
    # L1 solana #2: Jupiter-priced, no ticks -> peak from marks (exit-dependent), +30%
    sig(L1, "TOK_B", T0 + 1000, 500, 1.0)
    pos(2, "solana", "TOK_B", L1, 500, 1.0, T0 + 1001, 1.3, T0 + 1100, {"filled_at": T0 + 1001, "fill_model": "jupiter+0.8s"})
    db.execute("INSERT INTO marks VALUES(2,?,1.1)", (T0 + 1050,))
    db.execute("INSERT INTO marks VALUES(2,?,1.4)", (T0 + 1080,))
    # L1 solana #3: still open -> counted in `open`, not in PnL
    pos(3, "solana", "TOK_C", L1, 10, 2.0, T0 + 2000, None, None, {}, status="open")
    # L1 xchain: one closed on robinhood (+$50) with two buy fills (gaps +4 and +8), one closed on bsc (-$100)
    pos(4, "robinhood", "0xaaa", L1, 10, 200.0, T0 + 3000, 250.0, T0 + 4000, {"xchain": 1})
    db.execute("INSERT INTO xchain_fills(ts,position_id,chain,token,side,gap_pct) VALUES(?,4,'robinhood','0xaaa','buy',4.0)", (T0 + 3000,))
    db.execute("INSERT INTO xchain_fills(ts,position_id,chain,token,side,gap_pct) VALUES(?,4,'robinhood','0xaaa','buy',8.0)", (T0 + 3100,))
    db.execute("INSERT INTO marks VALUES(4,?,300.0)", (T0 + 3500,))
    pos(5, "bsc", "0xbbb", L1, 10, 200.0, T0 + 5000, 100.0, T0 + 6000, {"xchain": 1})
    # L2: only solana, two trades, the second one is the runner (+3.0 SOL)
    sig(L2, "TOK_D", T0 + 100, 100, 1.0)
    pos(6, "solana", "TOK_D", L2, 100, 2.0, T0 + 101, 1.7, T0 + 200, {"filled_at": T0 + 101, "fill_model": "pumpfun_curve+0.8s"})
    sig(L2, "TOK_E", T0 + 300, 100, 1.0)
    pos(7, "solana", "TOK_E", L2, 100, 2.0, T0 + 301, 5.0, T0 + 900, {"filled_at": T0 + 301, "fill_model": "pumpswap_amm+1.5s"})
    # a live (paper=0) position must never count
    db.execute("INSERT INTO positions(id,chain,token,leader,token_amount,cost,opened_at,paper,status,exit_value,closed_at) "
               "VALUES(8,'solana','TOK_F',?,'1',1.0,?,0,'closed',9.0,?)", (L2, T0 + 400, T0 + 500))
    db.commit()
    db.close()


def main():
    path = os.path.join(tempfile.mkdtemp(), "t.db")
    seed(path)
    db = open_db(path)
    st = collect(db, peak_h=6.0)

    s = st[(L1, "solana")]
    assert s["trades"] == 2 and s["open"] == 1 and s["wins"] == 1, s
    assert abs(s["pnl"] - (-0.4 + 0.3)) < 1e-9 and abs(s["wo_best"] - (-0.4)) < 1e-9
    assert abs(s["avg_pct"] - 5.0) < 1e-9 and abs(s["med_pct"] - 5.0) < 1e-9       # (-20 + 30) / 2
    assert s["venues"] == {"pool": 1, "jup": 1}
    # peak: TOK_A 2.5x from ticks within 6 h (the 9x tick at 7 h is outside), TOK_B 1.4/1.0 from marks -> median 95%
    assert abs(s["peak"] - (150 + 40) / 2) < 1e-9 and s["peak_n"] == 2 and s["peak_marks"] == 1, s
    # entry gap: TOK_A ours 2/1900 vs his 1/1000 = +5.26%; TOK_B 1/500 vs 1/500 = 0 -> median 2.63
    assert abs(s["gap"] - 100 * ((2 / 1900) / (1 / 1000) - 1) / 2) < 1e-9, s["gap"]
    assert s["unit"] == "SOL"

    x = st[(L1, "xchain")]
    assert x["trades"] == 2 and x["wins"] == 1 and x["pnl"] == -50.0 and x["wo_best"] == -100.0
    assert x["gap"] == 6.0 and x["venues"] == {"relay": 2} and x["unit"] == "USD"
    assert x["peak"] == 50.0 and x["peak_marks"] == 1 and x["peak_n"] == 1        # bsc one has no marks

    a = st[(L1, "all")]
    assert a["trades"] == 4 and a["open"] == 1 and a["pnl"] is None and a["wo_best"] is None   # no --sol-usd
    assert a["venues"] == {"pool": 1, "jup": 1, "relay": 2}
    a = collect(db, peak_h=6.0, sol_usd=100.0)[(L1, "all")]
    assert abs(a["pnl"] - ((-0.4 + 0.3) * 100 - 50)) < 1e-9 and a["wins"] == 2    # -10 USD solana, -50 xchain
    assert abs(a["wo_best"] - (a["pnl"] - 50.0)) < 1e-9                          # best = robinhood +$50

    t = st[(L2, "solana")]
    assert t["trades"] == 2 and abs(t["pnl"] - 2.7) < 1e-9 and abs(t["wo_best"] - (-0.3)) < 1e-9
    assert t["venues"] == {"pool": 2} and (L2, "xchain") not in st and (L2, "all") not in st
    assert "TOK_F" not in str(st)                                               # live position ignored

    # filters: --from-id and --hours (relative to now: everything here is years old -> empty)
    assert set(collect(db, from_id=6)) == {(L2, "solana")}
    assert collect(db, hours=1.0) == {}
    print("leader_report: PnL, w/o best, peak (ticks window vs marks), entry gap, venues, open, all-scope, filters OK")


if __name__ == "__main__":
    main()
