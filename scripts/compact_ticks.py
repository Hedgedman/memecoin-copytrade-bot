"""Compact old raw ticks into 1-minute bars. MANUAL - run it only with the bot STOPPED (it writes to the DB).

    python -m scripts.compact_ticks [--db copybot.db] [--days 7] [--dry-run] [--no-vacuum]

Raw `ticks` older than --days (default 7) are aggregated per (token, venue, minute) into `ticks_1m`:
    ts (minute start), token, venue, n (raw ticks in the bar),
    tok_open/sol_open (first state), tok_high/sol_high (state at the highest price), tok_low/sol_low (lowest),
    tok_close/sol_close (last state), real_tok_close/real_sol_close (last curve caps)
then the raw rows are deleted and the database is VACUUMed (unless --no-vacuum). whatif_ticksv2.py replays
positions without raw ticks on these bars (4 points per minute, minute precision).
Bars already present for a (token, venue, minute) are replaced from the raw rows (idempotent re-runs).
"""
import argparse
import os
import sqlite3
import time

BAR_SQL = """
INSERT INTO ticks_1m(ts, token, venue, n, tok_open, sol_open, tok_high, sol_high, tok_low, sol_low,
                     tok_close, sol_close, real_tok_close, real_sol_close)
SELECT b.m, b.token, b.venue, b.n,
       (SELECT tok_res FROM ticks t WHERE t.token=b.token AND t.venue=b.venue AND t.ts>=b.m AND t.ts<b.m+60 ORDER BY t.ts LIMIT 1),
       (SELECT sol_res FROM ticks t WHERE t.token=b.token AND t.venue=b.venue AND t.ts>=b.m AND t.ts<b.m+60 ORDER BY t.ts LIMIT 1),
       (SELECT tok_res FROM ticks t WHERE t.token=b.token AND t.venue=b.venue AND t.ts>=b.m AND t.ts<b.m+60 AND t.tok_res>0 ORDER BY 1.0*t.sol_res/t.tok_res DESC LIMIT 1),
       (SELECT sol_res FROM ticks t WHERE t.token=b.token AND t.venue=b.venue AND t.ts>=b.m AND t.ts<b.m+60 AND t.tok_res>0 ORDER BY 1.0*t.sol_res/t.tok_res DESC LIMIT 1),
       (SELECT tok_res FROM ticks t WHERE t.token=b.token AND t.venue=b.venue AND t.ts>=b.m AND t.ts<b.m+60 AND t.tok_res>0 ORDER BY 1.0*t.sol_res/t.tok_res ASC LIMIT 1),
       (SELECT sol_res FROM ticks t WHERE t.token=b.token AND t.venue=b.venue AND t.ts>=b.m AND t.ts<b.m+60 AND t.tok_res>0 ORDER BY 1.0*t.sol_res/t.tok_res ASC LIMIT 1),
       (SELECT tok_res FROM ticks t WHERE t.token=b.token AND t.venue=b.venue AND t.ts>=b.m AND t.ts<b.m+60 ORDER BY t.ts DESC LIMIT 1),
       (SELECT sol_res FROM ticks t WHERE t.token=b.token AND t.venue=b.venue AND t.ts>=b.m AND t.ts<b.m+60 ORDER BY t.ts DESC LIMIT 1),
       (SELECT real_tok FROM ticks t WHERE t.token=b.token AND t.venue=b.venue AND t.ts>=b.m AND t.ts<b.m+60 ORDER BY t.ts DESC LIMIT 1),
       (SELECT real_sol FROM ticks t WHERE t.token=b.token AND t.venue=b.venue AND t.ts>=b.m AND t.ts<b.m+60 ORDER BY t.ts DESC LIMIT 1)
FROM (SELECT CAST(ts / 60 AS INTEGER) * 60 AS m, token, venue, COUNT(*) AS n FROM ticks
      WHERE ts < ? AND tok_res > 0 GROUP BY 1, 2, 3) b
"""

SCHEMA = """
CREATE TABLE IF NOT EXISTS ticks_1m(
    ts REAL, token TEXT, venue TEXT, n INTEGER,
    tok_open INTEGER, sol_open INTEGER, tok_high INTEGER, sol_high INTEGER,
    tok_low INTEGER, sol_low INTEGER, tok_close INTEGER, sol_close INTEGER,
    real_tok_close INTEGER, real_sol_close INTEGER);
CREATE INDEX IF NOT EXISTS ticks_1m_token_ts ON ticks_1m(token, ts);
"""


def compact(db: sqlite3.Connection, cutoff: float, vacuum: bool = True, dry_run: bool = False) -> dict:
    """Aggregate ticks with ts < cutoff into ticks_1m and delete them. -> {raw, bars, deleted}."""
    db.executescript(SCHEMA)
    raw = db.execute("SELECT COUNT(*) FROM ticks WHERE ts < ?", (cutoff,)).fetchone()[0]
    bars = db.execute("SELECT COUNT(*) FROM (SELECT 1 FROM ticks WHERE ts < ? AND tok_res > 0 GROUP BY "
                      "CAST(ts / 60 AS INTEGER), token, venue)", (cutoff,)).fetchone()[0]
    if dry_run or not raw:
        return {"raw": raw, "bars": bars, "deleted": 0}
    db.execute("BEGIN")
    db.execute("DELETE FROM ticks_1m WHERE ts < ? AND (token, venue, ts) IN (SELECT token, venue, "
               "CAST(ts / 60 AS INTEGER) * 60 FROM ticks WHERE ts < ?)", (cutoff, cutoff))
    db.execute(BAR_SQL, (cutoff,))
    deleted = db.execute("DELETE FROM ticks WHERE ts < ?", (cutoff,)).rowcount
    db.commit()
    if vacuum:
        db.execute("VACUUM")
    return {"raw": raw, "bars": bars, "deleted": deleted}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default="copybot.db")
    ap.add_argument("--days", type=float, default=7.0, help="keep raw ticks younger than this")
    ap.add_argument("--dry-run", action="store_true", help="only count what would be compacted")
    ap.add_argument("--no-vacuum", action="store_true")
    a = ap.parse_args()
    path = os.path.abspath(os.path.expanduser(a.db))
    size0 = os.path.getsize(path)
    db = sqlite3.connect(path, timeout=5)
    try:
        db.execute("BEGIN IMMEDIATE"); db.execute("ROLLBACK")     # fails fast if the bot still holds the DB
    except sqlite3.OperationalError as e:
        raise SystemExit(f"database busy ({e}): stop the bot first")
    cutoff = time.time() - a.days * 86400
    r = compact(db, cutoff, vacuum=not a.no_vacuum, dry_run=a.dry_run)
    db.close()
    print(f"{'would compact' if a.dry_run else 'compacted'} {r['raw']:,} raw ticks older than {a.days:g} days into "
          f"{r['bars']:,} 1-minute bars ({r['deleted']:,} raw rows deleted); "
          f"db {size0 / 1e6:,.1f} MB -> {os.path.getsize(path) / 1e6:,.1f} MB")


if __name__ == "__main__":
    main()
