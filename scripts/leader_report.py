"""Leader scorecard (read-only on copybot.db): who is worth copying, Solana and cross-chain, per leader.

Usage (from the copybot folder):
    python -m scripts.leader_report [--hours 168 | --from-id 150] [--min-trades 3] [--sol-usd 180] [--db copybot.db]

Rows: one per leader and scope - solana (paper positions in SOL), xchain (robinhood/bsc paper positions in
USD) and all (both). Judge leaders on `peak` (how far the token ran after OUR fill, exit-independent), not
on PnL: one runner flips a leader from worst to best, and `w/o best` shows how much of the PnL is that one
trade. Columns:
  trades    closed positions (open ones are counted in `open` and not in the PnL)
  wins      closed with exit > cost
  PnL       sum of exit - cost (SOL on solana, USD on xchain; `all` only with --sol-usd)
  w/o best  the same PnL without the best single trade (`-` with fewer than 2 trades)
  avg/med % per-trade return, mean and median
  peak      median of the highest price after our fill (relative to the first pool tick at/after the fill),
            Solana: pool ticks up to --peak-h after the fill, so an early stop does not hide a runner;
            xchain: Relay marks (every mark_interval_s) until we closed, so it IS exit-dependent (marked *)
  gap       median entry gap: our fill price vs the leader's (+ = we paid more). Solana: the leader's
            SOL/token from his swap vs our cost/tokens (incl. the paper fee); xchain: xchain_fills.gap_pct
  venues    Solana fill model: pool (pumpfun_curve / pumpswap_amm maths) vs jup (Jupiter-quoted)
  flag      SMALL below --small (default 10) closed trades: the row says nothing yet
DATA RULE: positions flagged by scripts/flags.py (unconfirmed single-step mark change > 50%) are excluded by
default and listed; --include-flagged keeps them.
"""
import argparse
import json
import os
import sqlite3
import statistics
import time
from collections import defaultdict
from datetime import datetime

from scripts.flags import flagged_note, flagged_positions

SMALL = 10


def med(xs):
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else None


def mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def f(x, spec, unit=""):
    return "-" if x is None else f"{x:{spec}}{unit}"


def open_db(path):
    db = sqlite3.connect(f"file:{os.path.abspath(os.path.expanduser(path))}?mode=ro", uri=True, timeout=10)
    db.execute("PRAGMA busy_timeout=10000")           # the bot writes ticks every second
    return db


def solana_peak(db, token, filled_at, peak_h):
    """Peak in % over the first pool tick at/after the fill, ticks in [filled_at, filled_at + peak_h]."""
    until = filled_at + peak_h * 3600
    first = db.execute("SELECT 1.0*sol_res/tok_res FROM ticks WHERE token=? AND ts>=? AND ts<=? AND tok_res>0 "
                       "ORDER BY ts LIMIT 1", (token, filled_at, until)).fetchone()
    if not first or not first[0]:
        return None
    top = db.execute("SELECT MAX(1.0*sol_res/tok_res) FROM ticks WHERE token=? AND ts>=? AND ts<=? AND tok_res>0",
                     (token, filled_at, until)).fetchone()[0]
    return 100 * (top / first[0] - 1)


def marks_peak(db, pid, cost):
    """Peak in % over cost from our own marks (stop at the close: exit-dependent)."""
    top = db.execute("SELECT MAX(value) FROM marks WHERE position_id=?", (pid,)).fetchone()[0]
    return None if top is None or not cost else 100 * (top / cost - 1)


def solana_gap(db, p):
    """Our fill price vs the leader's price from his swap (signal matched by leader+token+time)."""
    row = db.execute("SELECT quote_amount, token_amount FROM signals WHERE leader=? AND token=? AND side='buy' "
                     "AND action='bought' AND ABS(ts-?)<60 ORDER BY ABS(ts-?) LIMIT 1",
                     (p["leader"], p["token"], p["opened_at"], p["opened_at"])).fetchone()
    try:
        his = float(row[0]) / int(row[1])
        ours = float(p["cost"]) / int(p["token_amount"])
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    return 100 * (ours / his - 1) if his > 0 else None


def collect(db, from_id=0, hours=None, peak_h=6.0, sol_usd=None, flagged=None, paper=1):
    """-> {(leader, scope): stats}. scope in ('solana', 'xchain', 'all'). flagged = {id: reason} to leave out."""
    since = time.time() - hours * 3600 if hours else 0.0
    cols = [r[1] for r in db.execute("PRAGMA table_info(positions)")]
    rows = [dict(zip(cols, r)) for r in db.execute(
        "SELECT * FROM positions WHERE paper=? AND id>=? AND opened_at>=? ORDER BY id", (int(paper), from_id, since))
        if r[0] not in (flagged or {})]
    per = defaultdict(lambda: {"pnl": [], "pct": [], "peak": [], "gap": [], "open": 0, "venues": defaultdict(int),
                               "peak_marks": 0})
    for p in rows:
        t = json.loads(p.get("timing") or "{}")
        scope = "solana" if p["chain"] == "solana" else "xchain"
        s = per[(p["leader"], scope)]
        if p["status"] != "closed" or p["exit_value"] is None:
            s["open"] += 1
            continue
        cost = float(p["cost"] or 0)
        pnl = float(p["exit_value"]) - cost
        s["pnl"].append(pnl)
        s["pct"].append(100 * pnl / cost if cost else None)
        if scope == "solana":
            filled = t.get("filled_at") or p["opened_at"]
            pk = solana_peak(db, p["token"], filled, peak_h)
            if pk is None:
                pk = marks_peak(db, p["id"], cost)
                s["peak_marks"] += pk is not None
            s["peak"].append(pk)
            s["gap"].append(solana_gap(db, p))
            model = (t.get("fill_model") or "?").split("+")[0]
            s["venues"]["jup" if model == "jupiter" else "?" if model == "?" else "pool"] += 1
        else:
            pk = marks_peak(db, p["id"], cost)
            s["peak"].append(pk)
            s["peak_marks"] += pk is not None
            gaps = [g for (g,) in db.execute("SELECT gap_pct FROM xchain_fills WHERE position_id=? AND side='buy'",
                                             (p["id"],))]
            s["gap"].append(med(gaps))
            s["venues"]["relay"] += 1
    out = {}
    for (leader, scope), s in per.items():
        out[(leader, scope)] = summarize(s, scope)
    for leader in {k[0] for k in per}:
        both = [per[(leader, sc)] for sc in ("solana", "xchain") if (leader, sc) in per]
        if len(both) < 2:
            continue
        unit = [1.0 if sc == "xchain" else sol_usd for sc in ("solana", "xchain") if (leader, sc) in per]
        comb = {"pct": sum((b["pct"] for b in both), []), "peak": sum((b["peak"] for b in both), []),
                "gap": sum((b["gap"] for b in both), []), "open": sum(b["open"] for b in both),
                "venues": defaultdict(int), "peak_marks": sum(b["peak_marks"] for b in both),
                "pnl": sum(([x * u for x in b["pnl"]] for b, u in zip(both, unit)), []) if sol_usd else []}
        for b in both:
            for k, v in b["venues"].items():
                comb["venues"][k] += v
        out[(leader, "all")] = summarize(comb, "all", n=len(comb["pct"]),
                                         wins=sum(x > 0 for b in both for x in b["pnl"]))
    return out


def summarize(s, scope, n=None, wins=None):
    pnl = s["pnl"]
    n = len(pnl) if n is None else n
    return {"trades": n, "open": s["open"], "wins": sum(x > 0 for x in pnl) if wins is None else wins,
            "pnl": sum(pnl) if pnl else None,
            "wo_best": (sum(pnl) - max(pnl)) if len(pnl) >= 2 else None,
            "avg_pct": mean(s["pct"]), "med_pct": med(s["pct"]), "peak": med(s["peak"]),
            "peak_n": sum(x is not None for x in s["peak"]), "peak_marks": s["peak_marks"],
            "gap": med(s["gap"]), "venues": dict(s["venues"]), "unit": {"solana": "SOL", "xchain": "USD"}.get(scope, "USD")}


def venues_str(v):
    return " ".join(f"{k} {n}" for k, n in sorted(v.items(), key=lambda kv: -kv[1])) or "-"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default="copybot.db")
    ap.add_argument("--from-id", type=int, default=0, help="positions with id >= this (e.g. since a restart)")
    ap.add_argument("--hours", type=float, default=None, help="positions opened in the last N hours (default: all)")
    ap.add_argument("--min-trades", type=int, default=1, help="hide leaders with fewer closed trades (all scopes)")
    ap.add_argument("--small", type=int, default=SMALL, help="flag rows with fewer closed trades than this")
    ap.add_argument("--peak-h", type=float, default=6.0, help="Solana: ticks window after the fill for the peak")
    ap.add_argument("--sol-usd", type=float, default=None, help="SOL price to combine Solana + xchain PnL in USD")
    ap.add_argument("--include-flagged", action="store_true", help="keep positions the DATA RULE flags")
    ap.add_argument("--live", action="store_true", help="LIVE positions (paper=0) instead of paper positions")
    a = ap.parse_args()
    db = open_db(a.db)
    flagged = flagged_positions(db)
    print(flagged_note(flagged, a.include_flagged))
    for pid in sorted(flagged):
        print(f"  #{pid:<5} {flagged[pid]}")
    stats = collect(db, a.from_id, a.hours, a.peak_h, a.sol_usd, None if a.include_flagged else flagged,
                    paper=0 if a.live else 1)
    when = f"last {a.hours:g} h" if a.hours else "all history"
    if a.from_id:
        when += f", positions >= #{a.from_id}"
    print(f"LEADER SCORECARD - {when} - {'LIVE' if a.live else 'paper'} positions, closed only (open shown separately)")
    print("peak = median highest price after our fill (Solana: pool ticks, exit-independent; * = from marks, "
          "exit-dependent)\n")
    hdr = (f"{'leader':<9} {'scope':<7} {'trades':>6} {'open':>4} {'wins':>4} {'PnL':>11} {'w/o best':>11} "
           f"{'avg %':>7} {'med %':>7} {'peak':>8} {'gap':>7}  {'venues':<16} flag")
    print(hdr)
    leaders = {}                                     # leader -> closed trades over both chains
    for (leader, scope), s in stats.items():
        leaders[leader] = max(leaders.get(leader, 0), s["trades"])
    order = sorted(leaders, key=lambda l: (-leaders[l], l))
    shown = 0
    for leader in order:
        if leaders[leader] < a.min_trades:
            continue
        for scope in ("solana", "xchain", "all"):
            s = stats.get((leader, scope))
            if not s:
                continue
            pnl = f"{s['pnl']:+,.2f} {s['unit']}" if s["pnl"] is not None else ("-" if scope != "all" else "(--sol-usd)")
            wob = f"{s['wo_best']:+,.2f}" if s["wo_best"] is not None else "-"
            peak = f(s["peak"], "+.0f", "%") + ("*" if s["peak_marks"] and s["peak_marks"] == s["peak_n"] else
                                                  "~" if s["peak_marks"] else "")
            flag = "SMALL" if s["trades"] < a.small else ""
            print(f"{leader[:8]:<9} {scope:<7} {s['trades']:>6} {s['open']:>4} {s['wins']:>4} {pnl:>11} {wob:>11} "
                  f"{f(s['avg_pct'], '+.1f'):>7} {f(s['med_pct'], '+.1f'):>7} {peak:>8} {f(s['gap'], '+.1f', '%'):>7}  "
                  f"{venues_str(s['venues']):<16} {flag}")
        shown += 1
    if not shown:
        print(f"  no closed {'live' if a.live else 'paper'} positions in this window")
    ns = sum(s["trades"] for (l, sc), s in stats.items() if sc == "solana")
    nx = sum(s["trades"] for (l, sc), s in stats.items() if sc == "xchain")
    ps = sum(s["pnl"] or 0 for (l, sc), s in stats.items() if sc == "solana")
    px = sum(s["pnl"] or 0 for (l, sc), s in stats.items() if sc == "xchain")
    print(f"\n{'total':<17} solana {ns} trades {ps:+,.2f} SOL | xchain {nx} trades {px:+,.2f} USD | "
          f"{len([l for l in leaders if leaders[l] >= a.small])} of {len(leaders)} leaders have >= {a.small} trades")
    print(f"  (sorted by trades; `~` = peak partly from marks; gap on Solana includes the paper fee; "
          f"generated {datetime.now():%Y-%m-%d %H:%M})")


if __name__ == "__main__":
    main()
