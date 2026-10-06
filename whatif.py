"""What-if analysis: replay every paper position's recorded price path with other TP/SL settings.

Usage (from the copybot folder):
    python whatif.py                 # all closed paper positions
    python whatif.py --from-id 4     # skip positions 1-3 (old stop logic)

How it works
- Each position has a recorded series of exit quotes ("marks"): what selling everything
  would have returned at that moment. The first mark is the stop reference ("entry price").
- For each TP/SL pair we walk through the marks and exit at the first mark that hits the
  take-profit (vs. cost) or the stop (vs. the first mark). Exit value = mark - sell fee.

Honest limits - read before trusting the numbers
- Marks were taken every few seconds (or slower when rate-limited). Real prices between
  two marks are unknown, so stops here fill as late as the bot's checks did.
- CENSORED: if a simulated setting would hold longer than the real position did (e.g. a
  higher TP than you actually used), the price path after the real exit is unknown. Those
  trades are valued at their last mark and counted in the "cens" column. If many trades in
  a cell are censored, that cell is guesswork.
- Small samples overfit. With < 30 trades, use this for direction, not for exact settings.
"""
from __future__ import annotations

import argparse
import sqlite3

import yaml

TPS = [0.05, 0.10, 0.15, 0.20, 0.30, 0.50, 1.00]
SLS = [0.05, 0.10, 0.15, 0.20, 0.30]


def load(db_path: str, from_id: int):
    db = sqlite3.connect(db_path)
    pos = db.execute("SELECT id, cost FROM positions WHERE paper=1 AND status='closed' AND id>=? "
                     "ORDER BY id", (from_id,)).fetchall()
    out = []
    for pid, cost in pos:
        marks = [v for (v,) in db.execute("SELECT value FROM marks WHERE position_id=? ORDER BY ts", (pid,))]
        if marks:
            out.append((pid, cost, marks))
    return out


def simulate(cost: float, marks: list[float], tp: float, sl: float, fee: float):
    """Return (exit_value, censored)."""
    ref = marks[0]
    for v in marks:
        if v / cost - 1 >= tp:
            return v - fee, False
        if v / ref - 1 <= -sl:
            return v - fee, False
    return marks[-1] - fee, True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="copybot.db")
    ap.add_argument("-c", "--config", default="config.yaml")
    ap.add_argument("--from-id", type=int, default=1)
    args = ap.parse_args()

    try:
        fee = float(yaml.safe_load(open(args.config))["chains"]["solana"].get("paper_fee_per_tx_sol", 0))
    except Exception:
        fee = 0.0
    data = load(args.db, args.from_id)
    if not data:
        print("No closed paper positions with recorded prices yet.")
        return

    print(f"{len(data)} positions (from id {args.from_id}), sell fee {fee} SOL per exit\n")
    print("Cell = total PnL in SOL, wins/trades, c = censored trades")
    header = "SL \\ TP".ljust(8) + "".join(f"{f'+{int(tp*100)}%':>17}" for tp in TPS)
    print(header)
    print("-" * len(header))
    best = None
    for sl in SLS:
        row = f"-{int(sl*100)}%".ljust(8)
        for tp in TPS:
            pnl = wins = cens = 0
            for _, cost, marks in data:
                value, c = simulate(cost, marks, tp, sl, fee)
                pnl += value - cost
                wins += value > cost
                cens += c
            cell = f"{pnl:+.2f} {wins}/{len(data)} c{cens}"
            row += f"{cell:>17}"
            if cens <= len(data) * 0.2 and (best is None or pnl > best[0]):
                best = (pnl, tp, sl, cens)
        print(row)

    actual = sum(v for (v,) in sqlite3.connect(args.db).execute(
        "SELECT exit_value - cost FROM positions WHERE paper=1 AND status='closed' AND id>=?", (args.from_id,)))
    print(f"\nActual result of these positions: {actual:+.2f} SOL")
    if best:
        print(f"Best cell with <=20% censored trades: TP +{int(best[1]*100)}% / SL -{int(best[2]*100)}% "
              f"-> {best[0]:+.2f} SOL ({best[3]} censored)")
    if len(data) < 30:
        print(f"\nOnly {len(data)} trades: treat this as a direction, not as the right settings.")


if __name__ == "__main__":
    main()
