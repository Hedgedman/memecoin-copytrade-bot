"""One-off, read-only: list the positions the DATA RULE flags (scripts/flags.py) and write them as JSON.

    python -m scripts.flag_positions [--db copybot.db] [--out flagged_positions.json] [--threshold 0.5]

Writes a JSON list [{"id", "chain", "leader", "token", "reason"}] (never the database). Expected on the
Oct 8 2026 database: 40, 192, 269, 299.
"""
import argparse
import json
import os
import sqlite3

from scripts.flags import THRESHOLD, flagged_positions


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default="copybot.db")
    ap.add_argument("--out", default="flagged_positions.json")
    ap.add_argument("--threshold", type=float, default=THRESHOLD)
    a = ap.parse_args()
    db = sqlite3.connect(f"file:{os.path.abspath(os.path.expanduser(a.db))}?mode=ro", uri=True)
    db.execute("PRAGMA busy_timeout=10000")
    flagged = flagged_positions(db, a.threshold)
    rows = []
    for pid in sorted(flagged):
        chain, leader, token = db.execute("SELECT chain, leader, token FROM positions WHERE id=?", (pid,)).fetchone()
        rows.append({"id": pid, "chain": chain, "leader": leader, "token": token, "reason": flagged[pid]})
        print(f"#{pid:<5} {chain:<9} {leader[:8]}  {token[:12]}  {flagged[pid]}")
    with open(a.out, "w") as f:
        json.dump(rows, f, indent=1)
    print(f"{len(rows)} flagged position(s) -> {a.out}")


if __name__ == "__main__":
    main()
