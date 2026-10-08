"""LIVE vs PAPER side by side (read-only on both DBs) - the daily check of H8-LIVE:

    python -m scripts.live_vs_paper [--live copybot.db] [--paper ~/Documents/copybot-dev/copybot.db]
                                    [--from-id 55] [--window 120] [-c config.yaml]

For every LIVE position (paper=0 in the live DB) the PAPER position of the same leader + token in the dev paper
DB opened within --window seconds = the same leader signal. Per row:
  fill live/paper   SOL per token we paid (cost / tokens, all-in: real fees live, paper_fee_per_tx_sol paper)
  fill gap          100 x (live fill price / paper fill price - 1): + = live paid more per token
  leader gap        100 x (our fill price / the leader's price from his swap - 1), live and paper
  exit              reason + return on cost, live and paper; stop rows compare the realized stop fills
  points            live return - paper return in percentage points of cost (exit comparison)
Summary: realized total, today's realized vs the daily loss limit (UTC day), medians, and the H8-LIVE kill /
pass rules (LIVE_CHECKLIST.md). Note: the dev paper bot buys 2.0 SOL vs 1.0 SOL live, so its fill carries more
price impact: a live fill gap near 0 or slightly negative is the expected baseline."""
from __future__ import annotations

import argparse
import os
import sqlite3
import statistics
import time
from datetime import datetime, timezone

import yaml


def open_ro(path: str) -> sqlite3.Connection:
    db = sqlite3.connect(f"file:{os.path.abspath(os.path.expanduser(path))}?mode=ro", uri=True, timeout=10)
    db.execute("PRAGMA busy_timeout=10000")
    db.row_factory = sqlite3.Row
    return db


def leader_price(db, leader: str, token: str, opened_at: float) -> float | None:
    """The leader's SOL per raw token from his bought signal next to our fill."""
    r = db.execute("SELECT quote_amount, token_amount FROM signals WHERE leader=? AND token=? AND side='buy' "
                   "AND action='bought' AND ABS(ts-?)<60 ORDER BY ABS(ts-?) LIMIT 1",
                   (leader, token, opened_at, opened_at)).fetchone()
    try:
        return float(r[0]) / int(r[1]) if r and int(r[1]) else None
    except (TypeError, ValueError):
        return None


def price(p) -> float | None:
    try:
        return float(p["cost"]) / int(p["token_amount"]) if int(p["token_amount"]) else None
    except (TypeError, ValueError):
        return None


def ret(p) -> float | None:
    return None if p["status"] != "closed" or p["exit_value"] is None or not p["cost"] else 100 * (float(p["exit_value"]) / float(p["cost"]) - 1)


def gap(a, b) -> float | None:
    return None if a is None or b is None or b <= 0 else 100 * (a / b - 1)


def compare(live_db, paper_db, from_id: int = 0, window: float = 120.0, now: float | None = None) -> tuple[list[dict], dict]:
    now = now or time.time()
    live_db.row_factory = paper_db.row_factory = sqlite3.Row
    rows = []
    for p in live_db.execute("SELECT * FROM positions WHERE paper=0 AND id>=? ORDER BY id", (from_id,)):
        q = paper_db.execute("SELECT * FROM positions WHERE paper=1 AND leader=? AND token=? AND ABS(opened_at-?)<=? "
                             "ORDER BY ABS(opened_at-?) LIMIT 1",
                             (p["leader"], p["token"], p["opened_at"], window, p["opened_at"])).fetchone()
        lp, pp = price(p), price(q) if q else None
        r = {"id": p["id"], "token": p["token"], "leader": p["leader"], "opened_at": p["opened_at"],
             "status": p["status"], "live_fill": lp, "paper_fill": pp, "fill_gap": gap(lp, pp),
             "live_leader_gap": gap(lp, leader_price(live_db, p["leader"], p["token"], p["opened_at"])),
             "paper_leader_gap": gap(pp, leader_price(paper_db, q["leader"], q["token"], q["opened_at"])) if q else None,
             "live_exit": p["exit_reason"], "paper_exit": q["exit_reason"] if q else None,
             "live_ret": ret(p), "paper_ret": ret(q) if q else None, "paper_id": q["id"] if q else None,
             "pnl": (float(p["exit_value"]) - float(p["cost"])) if p["status"] == "closed" and p["exit_value"] is not None else None}
        r["points"] = None if r["live_ret"] is None or r["paper_ret"] is None else r["live_ret"] - r["paper_ret"]
        rows.append(r)
    closed = [r for r in rows if r["pnl"] is not None]
    pnls = [r["pnl"] for r in closed]
    day0 = datetime.fromtimestamp(now, timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    today = live_db.execute("SELECT COALESCE(SUM(exit_value-cost),0) FROM positions WHERE paper=0 AND status='closed' "
                            "AND closed_at>=? AND NOT (timing LIKE '%\"profile\"%')", (day0,)).fetchone()[0]
    fill_gaps = [r["fill_gap"] for r in rows if r["fill_gap"] is not None]
    lgaps = [r["live_leader_gap"] for r in rows if r["live_leader_gap"] is not None]
    stop_pts = [r["points"] for r in closed if r["live_exit"] == "stop_loss" and r["paper_exit"] == "stop_loss" and r["points"] is not None]
    s = {"n": len(rows), "closed": len(closed), "open": len(rows) - len(closed), "matched": len(fill_gaps),
         "total": sum(pnls), "wo_best2": sum(sorted(pnls)[:-2]) if len(pnls) > 2 else None, "today": today,
         "median_fill_gap": statistics.median(fill_gaps) if fill_gaps else None,
         "mean_fill_gap": statistics.fmean(fill_gaps) if fill_gaps else None,
         "median_leader_gap": statistics.median(lgaps) if lgaps else None, "n_leader_gap": len(lgaps),
         "stop_points": statistics.fmean(stop_pts) if stop_pts else None, "n_stops": len(stop_pts)}
    # H8-LIVE rules (LIVE_CHECKLIST.md): kill on any; pass needs all three
    kill = []
    if s["total"] <= -4.0:
        kill.append(f"realized total {s['total']:+.2f} SOL <= -4.0")
    if s["closed"] >= 15 and s["total"] < 0:
        kill.append(f"{s['closed']} trades with total {s['total']:+.2f} < 0")
    if s["n_leader_gap"] >= 10 and s["median_leader_gap"] > 15:
        kill.append(f"median live entry gap {s['median_leader_gap']:+.1f}% > +15% over {s['n_leader_gap']} trades")
    if s["matched"] >= 10 and s["mean_fill_gap"] >= 5:
        kill.append(f"live fills {s['mean_fill_gap']:+.1f} points worse than paper over {s['matched']} matched trades")
    s["kill"] = kill
    s["pass"] = (s["closed"] >= 30 and s["wo_best2"] is not None and s["wo_best2"] > 0
                 and s["matched"] >= 10 and abs(s["mean_fill_gap"]) <= 5)
    return rows, s


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--live", default="copybot.db")
    ap.add_argument("--paper", default="~/Documents/copybot-dev/copybot.db")
    ap.add_argument("-c", "--config", default="config.yaml", help="for the daily loss limit")
    ap.add_argument("--from-id", type=int, default=0, help="live positions with id >= this (H8-LIVE: the first id after the start)")
    ap.add_argument("--window", type=float, default=120.0)
    a = ap.parse_args()
    rows, s = compare(open_ro(a.live), open_ro(a.paper), a.from_id, a.window)
    try:
        limit = float(yaml.safe_load(open(a.config))["chains"]["solana"].get("daily_loss_limit", 0))
    except (OSError, KeyError, TypeError):
        limit = 0.0
    f = lambda x, spec="+.1f", unit="%": "-" if x is None else f"{x:{spec}}{unit}"
    print(f"LIVE vs PAPER - live {a.live} (paper=0, id >= {a.from_id}) vs paper {a.paper} (same leader + token within {a.window:g} s)")
    print(f"{'id':>4} {'when (UTC)':<16} {'token':<9} {'leader':<9} {'fill live':>11} {'fill paper':>11} {'gap':>7} "
          f"{'ldr live':>8} {'ldr pap':>8}  {'exit live':<22} {'exit paper':<22} {'points':>7} {'pnl':>8}")
    for r in rows:
        when = datetime.fromtimestamp(r["opened_at"], timezone.utc).strftime("%m-%d %H:%M:%S")
        ex = lambda reason, rt: ("open" if reason is None and rt is None else f"{reason or '?'} {f(rt)}") if reason or rt is not None else "no match"
        print(f"{r['id']:>4} {when:<16} {r['token'][:8]:<9} {r['leader'][:8]:<9} {f(r['live_fill'], '.3e', ''):>11} "
              f"{f(r['paper_fill'], '.3e', ''):>11} {f(r['fill_gap']):>7} {f(r['live_leader_gap']):>8} {f(r['paper_leader_gap']):>8}  "
              f"{ex(r['live_exit'], r['live_ret']):<22} {ex(r['paper_exit'], r['paper_ret']) if r['paper_id'] else 'no match':<22} "
              f"{f(r['points'], '+.1f', ''):>7} {f(r['pnl'], '+.3f', ''):>8}")
    print(f"\n{s['n']} live positions ({s['closed']} closed, {s['open']} open), {s['matched']} matched with a paper position")
    print(f"realized total {s['total']:+.3f} SOL | without the best 2: {f(s['wo_best2'], '+.3f', ' SOL')} | "
          f"today (UTC) {s['today']:+.3f} SOL vs daily loss limit -{limit:g} SOL"
          f"{' -> LIMIT HIT, no new buys today' if limit and s['today'] <= -limit else ''}")
    print(f"fill gap live vs paper: median {f(s['median_fill_gap'])}, mean {f(s['mean_fill_gap'])} | "
          f"live entry gap vs leader: median {f(s['median_leader_gap'])} over {s['n_leader_gap']} | "
          f"stop fills live - paper: {f(s['stop_points'], '+.1f', ' points')} over {s['n_stops']} common stops")
    if s["kill"]:
        print("KILL RULE HIT: " + "; ".join(s["kill"]))
    elif s["pass"]:
        print("PASS: 30+ trades, total > 0 without the best 2, live fills within 5 points of paper")
    else:
        print("H8-LIVE: no kill rule hit, pass not reached yet")


if __name__ == "__main__":
    main()
