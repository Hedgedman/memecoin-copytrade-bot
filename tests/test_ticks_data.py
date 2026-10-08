"""Block 1.5-1.8 (Oct 8 2026): python -m tests.test_ticks_data

whatif_ticksv2: entry coverage rule, DATA-RULE flag exclusion (+ --include-flagged), 1-minute bar replay;
scripts/flags: the flag rule; scripts/compact_ticks: raw ticks -> bars, raw deleted; scripts/missed_runners."""
import json
import os
import sqlite3
import subprocess
import sys
import tempfile

from bot.store import Store
from scripts.compact_ticks import compact
from scripts.flags import flagged_positions
from scripts.missed_runners import after_exit, price_path
from whatif_ticksv2 import bars_to_ticks, load

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
T0 = 1_700_000_000.0
FEES = {"pumpfun_curve": 125, "pumpswap_amm": 100}
L = 10**9


def seed(path):
    st = Store(path)
    db = st.db

    def pos(pid, token, opened, closed, exit_value=1.6, timing=None, marks=()):
        db.execute("INSERT INTO positions(id,chain,token,leader,token_amount,cost,opened_at,paper,status,exit_value,"
                   "closed_at,exit_reason,timing) VALUES(?,'solana',?,'LEADER_A',?,2.0,?,1,'closed',?,?,'stop_loss',?)",
                   (pid, token, str(10**12), opened, exit_value, closed, json.dumps({"filled_at": opened, **(timing or {})})))
        for dt, v in marks:
            db.execute("INSERT INTO marks VALUES(?,?,?)", (pid, opened + dt, v))

    def tick(token, ts, sol, tok=10**12):
        db.execute("INSERT INTO ticks VALUES(?,?,?,?,?,?,?,?)", (ts, 1, token, "pumpswap_amm", tok, sol, None, None))

    # 1: covered (ticks from the fill on), price doubles after our close
    pos(1, "A", T0, T0 + 60, marks=[(1, 2.0), (30, 1.9), (60, 1.6)])
    for dt, sol in [(0, 100 * L), (30, 95 * L), (60, 80 * L), (120, 160 * L), (3600, 200 * L), (86400 + 10, 1500 * L)]:
        tick("A", T0 + dt, sol)
    # 2: ticks start 100 s after the fill (after the close): not replayable
    pos(2, "B", T0 + 1000, T0 + 1050, marks=[(1, 2.0), (50, 1.6)])
    for dt in (100, 130, 200):
        tick("B", T0 + 1000 + dt, 100 * L)
    # 3: flagged - one mark step of -70%, unconfirmed
    pos(3, "C", T0 + 2000, T0 + 2100, marks=[(1, 2.0), (50, 2.0), (99, 0.6)])
    for dt in (0, 50, 99):
        tick("C", T0 + 2000 + dt, 100 * L)
    # 4: the same step, but confirmed by a Jupiter calibration right after it (pool_factors)
    pos(4, "D", T0 + 3000, T0 + 3100, marks=[(1, 2.0), (50, 2.0), (99, 0.6)])
    for dt in (0, 50, 99):
        tick("D", T0 + 3000 + dt, 100 * L)
    db.execute("INSERT INTO pool_factors VALUES(?,?,?)", (T0 + 3000 + 120, "D", 1.02))
    # 5: flagged by the bot itself (source-switch guard)
    pos(5, "E", T0 + 4000, T0 + 4100, timing={"flagged": "source switch pumpfun_curve->jupiter x0.036"},
        marks=[(1, 2.0), (99, 1.9)])
    tick("E", T0 + 4000, 100 * L)
    # 6: old position whose raw ticks were compacted: only 1-minute bars exist
    pos(6, "F", T0 + 5000 + 30, T0 + 5000 + 200, marks=[(1, 2.0), (170, 1.6)])
    for m, (o, h, l, c) in enumerate([(100, 120, 90, 110), (110, 130, 105, 125), (125, 125, 60, 70), (70, 80, 65, 75),
                                       (75, 300, 70, 280), (280, 290, 200, 210), (210, 220, 190, 200)]):
        ts = T0 + 5000 + 60 * m
        db.execute("INSERT INTO ticks_1m VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (ts, "F", "pumpswap_amm", 4, 10**12, o * L, 10**12, h * L, 10**12, l * L, 10**12, c * L, None, None))
    db.commit()
    return st


def main():
    d = tempfile.mkdtemp()
    path = os.path.join(d, "t.db")
    st = seed(path)
    ro = sqlite3.connect(f"file:{path}?mode=ro", uri=True)

    fl = flagged_positions(ro)
    assert set(fl) == {3, 5} and "-70%" in fl[3] and fl[5].startswith("bot:"), fl
    print("flags: unconfirmed -70% step flagged, the same step confirmed by pool_factors not, bot flag kept")

    data, skipped, mc_out, excluded, dropped = load(path, 1, FEES, 0.01, 7 * 86400)
    ids = sorted(p.pid for p in data)
    assert ids == [1, 4, 6], ids
    assert dropped["not_covered"] == [(2, 100)] and sorted(pid for pid, *_ in dropped["flagged"]) == [3, 5]
    assert dropped["bars_1m"] == [6] and skipped == 0
    assert [p.resolution for p in data] == ["tick", "tick", "1m"]
    six = next(p for p in data if p.pid == 6)
    assert len(six.series) == 7 * 4 and six.series[0][0] == T0 + 5000           # 4 points per bar, from the bar before
    highs = [s[1].sol_res for s in six.series]
    assert max(highs) == 300 * L                                               # the bar high is in the series
    data, *_, dropped = load(path, 1, FEES, 0.01, 7 * 86400, exclude_flagged=False)
    assert sorted(p.pid for p in data) == [1, 3, 4, 5, 6] and dropped["flagged"] == []
    data, *_, dropped = load(path, 1, FEES, 0.01, 7 * 86400, cover_s=150)
    assert 2 in {p.pid for p in data} and dropped["not_covered"] == []
    # ticks after our close are used: position 1 keeps the 24 h tick
    one = next(p for p in data if p.pid == 1)
    assert one.series[-1][0] == T0 + 86400 + 10
    print("whatif: coverage rule (#2 not replayable), flagged excluded + listed, --include-flagged, 1-minute bars")

    # bars_to_ticks ordering: a bar whose high is closer to the open puts the high first
    pts = bars_to_ticks([(0, "pumpswap_amm", 3, 10, 100, 10, 110, 10, 50, 10, 60, None, None)])
    assert [p[3] for p in pts] == [100, 110, 50, 60]
    pts = bars_to_ticks([(0, "pumpswap_amm", 3, 10, 100, 10, 150, 10, 95, 10, 140, None, None)])
    assert [p[3] for p in pts] == [100, 95, 150, 140]

    # missed runners: token A doubled within 1 h and went 15x in 24 h after our close
    r = after_exit(price_path(ro, "A", T0 - 5, T0 + 7 * 86400), T0, T0 + 60)
    assert abs(r["1h"][0] - 2.0) < 1e-9 and abs(r["1h"][1] - 2.5) < 1e-9       # 200 vs entry 100, vs exit 80
    assert abs(r["24h"][0] - 15.0) < 1e-9 and r["24h"][2] == 86400 + 10 - 60 and r["7d"][0] == 15.0
    assert after_exit(price_path(ro, "B", T0 + 995, T0 + 9999), T0 + 1000, T0 + 1050) is None   # no tick at the fill
    r6 = after_exit(price_path(ro, "F", T0 + 5000, T0 + 9999), T0 + 5030, T0 + 5200)
    assert abs(r6["1h"][0] - 300 / 120) < 1e-9 and abs(r6["1h"][1] - 300 / 65) < 1e-9, r6   # bar points
    out = subprocess.run([sys.executable, "-m", "scripts.missed_runners", "--db", path, "--no-gecko"],
                         capture_output=True, text=True, cwd=ROOT)
    assert out.returncode == 0 and "RUNNER" in out.stdout and " 2 flagged left out" in out.stdout, out.stdout + out.stderr
    assert "LEADER_A" in out.stdout and " 15.0x/18.8x" in out.stdout, out.stdout
    print("missed_runners: peaks after exit vs entry/exit per window, runner flag, bars, no-data handling")

    # compaction: ticks older than the cutoff -> 1-minute bars, raw deleted; the what-if still replays them
    rw = sqlite3.connect(path)
    before = rw.execute("SELECT COUNT(*) FROM ticks").fetchone()[0]
    res = compact(rw, T0 + 2500, vacuum=False, dry_run=True)
    assert res["deleted"] == 0 and rw.execute("SELECT COUNT(*) FROM ticks").fetchone()[0] == before
    res = compact(rw, T0 + 2500, vacuum=True)
    assert res["deleted"] == 10 and res["bars"] == 9, res           # T0 is 20 s into a minute: A 3, B 3, C 3 bars
    bars = rw.execute("SELECT token, ts, n, sol_open, sol_high, sol_low, sol_close FROM ticks_1m WHERE token IN ('A','B') "
                      "ORDER BY token, ts").fetchall()
    first = bars[0]
    assert first[0] == "A" and first[1] == T0 // 60 * 60 and first[2] == 2 and (first[3], first[4], first[5], first[6]) == (
        100 * L, 100 * L, 95 * L, 95 * L), first
    assert rw.execute("SELECT COUNT(*) FROM ticks WHERE ts < ?", (T0 + 2500,)).fetchone()[0] == 0
    assert rw.execute("SELECT COUNT(*) FROM ticks").fetchone()[0] == before - 10
    res2 = compact(rw, T0 + 2500, vacuum=False)                       # idempotent
    assert res2["deleted"] == 0 and rw.execute("SELECT COUNT(*) FROM ticks_1m WHERE token='A'").fetchone()[0] == 3
    rw.close()
    data, *_, dropped = load(path, 1, FEES, 0.01, 7 * 86400, cover_s=30)
    assert 1 in {p.pid for p in data} and sorted(dropped["bars_1m"]) == [1, 2, 6]   # #2: bar slack covers it now
    assert next(p for p in data if p.pid == 1).resolution == "1m"
    print("compact_ticks: bars (open/high/low/close), raw rows deleted, idempotent, what-if replays on bars")
    print("ALL PASSED")


if __name__ == "__main__":
    main()
