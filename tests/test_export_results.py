"""Public results export: python -m tests.test_export_results

Synthetic paper + live databases (real schema via Store) with realistic-looking 44-character base58 wallet / token
strings and an EVM address. Checks the CSV columns and values, the period / venue / flag logic, and above all the
anonymization: after a full export no text output holds an address pattern, a leader or token string, or an 8-character
prefix of one, and the leak scan really fails (and writes nothing) when a pseudonym is an address."""
import json
import os
import re
import shutil
import tempfile

from bot.pricing.pools import PoolState
from bot.store import Store
from scripts import export_results as E

B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def addr(seed: int) -> str:
    """44 chars of base58, deterministic, looks like a Solana address."""
    return "".join(B58[(seed * 7 + i * 13 + i * i * seed) % len(B58)] for i in range(44))


LA, LB = addr(3), addr(5)
EVM = "0x59666dB7DEBFdB61A7ec3b5Be10c8034B79DbDd0"
TOK = {n: addr(100 + n) for n in range(1, 12)}
LMAP = {LA: "Leader-A", LB: "Leader-B", EVM.lower(): "Leader-A"}
T0 = 1_700_000_000.0
L = 1_000_000_000
BASE = PoolState("pumpswap_amm", 10**12, 100 * L)
K = BASE.sol_res * BASE.tok_res
TOKENS = BASE.buy_out(int(0.49 * L), 0)


def at(mult):
    sol = int(BASE.sol_res * mult ** 0.5)
    return PoolState("pumpswap_amm", K // sol, sol)


def seed_paper(path):
    st = Store(path)
    db = st.db

    def pos(pid, chain, token, leader, opened, exit_value, reason, timing, cost=0.5, status="closed", paper=1):
        closed = None if status != "closed" else opened + 120
        db.execute("INSERT INTO positions(id,chain,token,leader,token_amount,cost,opened_at,paper,status,exit_value,"
                   "closed_at,exit_reason,timing) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (pid, chain, token, leader, str(TOKENS), cost, opened, paper, status, exit_value, closed, reason,
                    json.dumps(timing)))

    def sig(leader, token, ts):                       # the leader paid 0.4 SOL for the same token amount
        db.execute("INSERT INTO signals(ts,chain,leader,side,token,token_amount,quote_amount,action,reason) "
                   "VALUES(?,'solana',?,'buy',?,?,?,'bought','mc $123,456 ok')", (ts, leader, token, str(TOKENS), 0.4))

    def ticks(token, ts, path):
        for dt, m in path:
            s = at(m)
            db.execute("INSERT INTO ticks VALUES(?,?,?,?,?,?,?,?)", (ts + dt, 1, token, "pumpswap_amm", s.tok_res, s.sol_res, 0, 0))

    up = [(0, 1.0), (5, 1.1), (10, 1.4), (20, 1.45), (60, 1.2), (600, 1.0)]
    down = [(0, 1.0), (5, 0.95), (10, 0.7), (20, 0.6), (600, 0.5)]
    fm = {"fill_model": "pumpswap_amm+0.8s", "fill_mc_usd": 120_000}
    # before #366 (ids < 366): a stop on a pool venue, a Jupiter-priced win, a profile position
    sig(LA, TOK[1], T0 + 1000); pos(230, "solana", TOK[1], LA, T0 + 1001, 0.4, "stop_loss", dict(fm, filled_at=T0 + 1001))
    ticks(TOK[1], T0 + 1001, down)
    pos(231, "solana", TOK[2], LB, T0 + 2001, 0.7, "take_profit", {"fill_model": "jupiter+0.8s", "fill_mc_usd": 40_000})
    pos(232, "solana", TOK[3], LB, T0 + 2501, 0.9, "take_profit", dict(fm, profile="mirror_" + LB[:8], filled_at=T0 + 2501))
    # the period boundary and after: a win on a pool, a bot-flagged one, a downtime-style one is tested separately
    sig(LB, TOK[4], T0 + 5000); pos(366, "solana", TOK[4], LB, T0 + 5001, 0.65, "take_profit", dict(fm, filled_at=T0 + 5001))
    ticks(TOK[4], T0 + 5001, up)
    sig(LA, TOK[5], T0 + 6000); pos(367, "solana", TOK[5], LA, T0 + 6001, 0.4, "stop_loss", dict(fm, filled_at=T0 + 6001))
    ticks(TOK[5], T0 + 6001, down)
    pos(368, "solana", TOK[6], LA, T0 + 7001, 5.0, "take_profit", dict(fm, flagged="source switch"))
    pos(369, "solana", TOK[7], LB, T0 + 8001, 0.2, "stop_loss", {"fill_model": "pumpfun_curve+0.8s", "fill_mc_usd": 29_000})
    pos(370, "solana", TOK[8], LB, T0 + 8501, None, None, {}, status="open")
    # EVM position of Leader-A (by his EVM address) with two buy fills (gaps +4 / +8)
    pos(371, "robinhood", "0x" + "ab" * 20, EVM, T0 + 9001, 250.0, "take_profit", {"xchain": 1}, cost=200.0)
    for ts, gap in ((T0 + 9001, 4.0), (T0 + 9101, 8.0)):
        db.execute("INSERT INTO xchain_fills(ts,position_id,chain,token,side,gap_pct) VALUES(?,371,'robinhood',?,'buy',?)",
                   (ts, "0x" + "ab" * 20, gap))
    db.commit()
    return st


def seed_live(path, opened_55):
    st = Store(path)
    db = st.db
    for pid, token, leader, opened, exitv, extra in (
            (1, TOK[2], LB, T0 + 100, 0.45, {"fill_model": "live", "quote_source": "pumpswap_amm", "fill_mc_usd": 50_000}),
            (55, TOK[5], LA, opened_55, 0.38, {"fill_model": "live", "quote_source": "pumpfun_curve*corr", "fill_mc_usd": 90_000,
                                               "buy_quoted_price": 1e-9, "buy_slippage_pct": -2.0})):
        db.execute("INSERT INTO positions(id,chain,token,leader,token_amount,cost,opened_at,entry_sig,paper,status,exit_value,"
                   "closed_at,exit_reason,exit_sig,timing) VALUES(?,?,?,?,?,?,?,?,0,'closed',?,?,?,?,?)",
                   (pid, "solana", token, leader, str(TOKENS), 0.5, opened, addr(900 + pid), exitv, opened + 30, "stop_loss",
                    addr(950 + pid), json.dumps(extra)))
    db.execute("INSERT INTO signals(ts,chain,leader,side,token,token_amount,quote_amount,action) "
               "VALUES(?,'solana',?,'buy',?,?,?,'bought')", (opened_55 - 1, LA, TOK[5], str(TOKENS), 0.4))
    db.commit()
    return st


def all_text(root):
    out = {}
    for d, _, files in os.walk(root):
        for fn in files:
            if fn.endswith((".csv", ".md")):
                out[os.path.join(d, fn)] = open(os.path.join(d, fn)).read()
    return out


def test_helpers():
    assert E.mc_bucket(None) == "n/a" and E.mc_bucket(29_999) == "<30k" and E.mc_bucket(30_000) == "30-100k"
    assert E.mc_bucket(499_999) == "250-500k" and E.mc_bucket(500_000) == ">=500k"
    s = E.pnl_stats([{"pnl": x, "pnl_pct": 10 * x} for x in (5, 1, -1, -2, -3)])
    assert s["n"] == 5 and s["wins"] == 2 and s["pnl"] == 0 and s["wo1"] == -5 and s["wo3"] == -5, s
    assert E.pseudonym(LA, LMAP) == "Leader-A" and E.pseudonym(EVM, LMAP) == "Leader-A"
    try:
        E.pseudonym(addr(77), LMAP)
        raise AssertionError("an unmapped leader must not be exported")
    except KeyError as e:
        assert addr(77) not in str(e)                          # the error message does not echo the address either
    print("helpers: market cap buckets, w/o best, pseudonym lookup refuses unmapped leaders")


def test_positions_and_anonymization():
    with tempfile.TemporaryDirectory() as d:
        paper, live, out = f"{d}/paper.db", f"{d}/live.db", f"{d}/results"
        seed_paper(paper)
        seed_live(live, T0 + 6002)
        findings = E.export_all(paper, live, LMAP, out, None)
        assert findings == [], findings
        files = sorted(os.path.relpath(p, out) for p in all_text(out))
        assert "RESULTS.md" in files and "positions_paper.csv" in files and "positions_live.csv" in files, files
        assert len([f for f in os.listdir(os.path.join(out, "charts")) if f.endswith(".png")]) == 5
        assert not os.path.exists(out + ".tmp")

        rows = [dict(zip(E.COLUMNS, line.split(","))) for line in open(f"{out}/positions_paper.csv").read().splitlines()[1:]]
        assert open(f"{out}/positions_paper.csv").readline().strip().split(",") == E.COLUMNS
        by = {int(r["id"]): r for r in rows}
        assert set(by) == {230, 231, 232, 366, 367, 368, 369, 371}, set(by)       # the open position is not exported
        assert by[230]["period"] == "before" and by[366]["period"] == "after" and by[371]["period"] == "after"
        assert by[230]["venue"] == "pumpswap" and by[231]["venue"] == "jupiter" and by[369]["venue"] == "curve"
        assert by[371]["venue"] == "evm" and by[371]["entry_gap_pct"] == "6.0" and by[371]["unit"] == "USD"
        assert by[230]["entry_mc_bucket"] == "100-250k" and by[231]["entry_mc_bucket"] == "30-100k"
        assert by[369]["entry_mc_bucket"] == "<30k" and by[371]["entry_mc_bucket"] == "n/a"
        assert by[230]["leader"] == "Leader-A" and by[366]["leader"] == "Leader-B" and by[371]["leader"] == "Leader-A"
        assert by[368]["flagged"] == "yes" and by[368]["flag_reason"] == "bot_flag" and by[230]["flagged"] == "no"
        assert by[232]["profile"] == "yes" and by[230]["profile"] == "no"
        assert by[230]["pnl_pct"] == "-20.0" and by[230]["hold_s"] == "120" and by[230]["exit_reason"] == "stop_loss"
        assert by[230]["entry_gap_pct"] == "25.0", by[230]["entry_gap_pct"]        # cost 0.5 vs the leader's 0.4
        assert all(re.fullmatch(r"\d{4}-\d{2}-\d{2}", r["opened_date"]) for r in rows)
        assert "token" not in E.COLUMNS and "signature" not in " ".join(E.COLUMNS)

        lrows = [dict(zip(E.COLUMNS, line.split(","))) for line in open(f"{out}/positions_live.csv").read().splitlines()[1:]]
        lby = {int(r["id"]): r for r in lrows}
        assert lby[1]["period"] == "before" and lby[55]["period"] == "after", lby     # placed by opening time vs paper #366
        assert lby[55]["venue"] == "curve" and lby[1]["venue"] == "pumpswap"          # live: quote_source of the fill

        # tables: Solana, non-flagged, non-profile only (230, 231, 366, 367, 369); H8 row for live #55 vs paper #367
        res = open(f"{out}/RESULTS.md").read()
        assert "Leader-A" in res and "Leader-B" in res and "live ids 55+" in res
        h5 = open(f"{out}/tables/h5_venue_split.md").read()
        assert "| #230+ | jupiter | 1 |" in h5, h5

        # anonymization: patterns, full strings, 8-char prefixes
        texts = all_text(out)
        secrets = [LA, LB, EVM, EVM.lower()] + list(TOK.values()) + [addr(900 + 1), addr(955)]
        for path, text in texts.items():
            assert not E.ADDR_RE.search(text), (path, E.ADDR_RE.search(text).group())
            for s in secrets:
                assert s not in text and s[:8] not in text, (path, s[:8])
        for png in os.listdir(os.path.join(out, "charts")):          # images: only metadata could carry text
            raw = open(os.path.join(out, "charts", png), "rb").read()
            assert not any(s[:12].encode() in raw for s in secrets), png
        assert sum(os.path.getsize(os.path.join(dp, f)) for dp, _, fs in os.walk(out) for f in fs) < 5_000_000
        print("export: columns, periods, venues, buckets, gaps, flags, profile, H5 / H8 / leader tables, no address in any output")


def test_downtime_flag():
    with tempfile.TemporaryDirectory() as d:
        st = Store(f"{d}/p.db")
        a, b = E.DOWNTIME
        for pid, opened, closed in ((1, a - 600, a - 300), (2, a - 600, b + 10), (3, b + 5, b + 100)):
            st.db.execute("INSERT INTO positions(id,chain,token,leader,token_amount,cost,opened_at,paper,status,exit_value,closed_at,"
                          "exit_reason,timing) VALUES(?,?,?,?,'1',1.0,?,1,'closed',1.0,?,'x','{}')",
                          (pid, "solana", TOK[1], LA, opened, closed))
        st.db.commit()
        rows = E.collect_positions(E.open_ro(f"{d}/p.db"), LMAP, ref_ts=0.0)
        assert [r["flag_reason"] for r in rows] == ["", "downtime", ""], rows
        print("downtime: only the position open across the dev bot downtime is flagged")


def test_leak_scan_blocks_publication():
    with tempfile.TemporaryDirectory() as d:
        paper, live, out = f"{d}/paper.db", f"{d}/live.db", f"{d}/results"
        seed_paper(paper)
        seed_live(live, T0 + 6002)
        bad = dict(LMAP)
        bad[LB] = TOK[3]                                          # a "pseudonym" that is a real token address
        findings = E.export_all(paper, live, bad, out, None)
        assert findings and any("address" in f for f in findings), findings
        assert not os.path.exists(out) and not os.path.exists(out + ".tmp")      # nothing was left behind
        # the scanner itself: pattern, and an 8-char prefix hidden inside a longer word
        os.makedirs(f"{d}/x")
        open(f"{d}/x/a.md", "w").write("clean text about Leader-A and 12.5 SOL\n")
        open(f"{d}/x/b.md", "w").write("see prefix%s_end\n" % TOK[2][:8])
        db = E.open_ro(paper)
        db.close()
        seed = Store(f"{d}/s.db")
        seed.db.execute("INSERT INTO positions(id,chain,token,leader,paper) VALUES(1,'solana',?,?,1)", (TOK[2], LA))
        seed.db.commit()
        found = E.leak_scan(f"{d}/x", [E.open_ro(f"{d}/s.db")])
        assert len(found) == 1 and found[0].endswith("b.md: 8-char prefix of a database address at offset 4"), found
        print("leak scan: address patterns and database prefixes are caught and nothing is published")


if __name__ == "__main__":
    test_helpers()
    test_positions_and_anonymization()
    test_downtime_flag()
    test_leak_scan_blocks_publication()
    print("ALL PASSED")
