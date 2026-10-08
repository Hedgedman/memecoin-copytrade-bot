"""Leader selection (read-only on copybot.db): CUT / PROMOTE / WATCH per leader, Solana + xchain combined.

    python -m scripts.leader_selection [--db copybot.db] [--from-id N] [--sol-usd 200] [--include-flagged] [--days 14]

Per leader (paper positions, Solana + robinhood/bsc together):
  entries     positions opened (Solana) + xchain opening fills; for a WATCH leader (no positions) his bought-or-
              skipped buy signals, so his entry gap can still be measured
  gap med     median entry gap, our fill price vs his (%); watch leaders / skipped signals: the first pool tick
              within 10 s of his signal vs his price (mid price, no fee/impact: a bit optimistic)
  gap>20%     share of entries with a gap above +20%
  closed      closed positions; PnL SOL (Solana) and PnL USD (xchain) separately; `w/o best` = combined USD PnL
              (Solana x --sol-usd) without the best trade; `w/o best 2` without the best two
  avg %       mean per-trade return; wins
  pool / jup  Solana PnL (SOL) of pool-priced vs Jupiter-priced positions (fill model)
  last        days since his last buy signal (any chain, bought or skipped)
Labels (rule shown):
  CUT      = median gap > +20% over >= 5 entries  OR  (>= 15 closed AND PnL w/o best < 0 AND avg < -5%)
             OR no buy signal in --days (14) days
  PROMOTE  = >= 30 closed AND PnL w/o best 2 > 0 AND median gap < +5%
  WATCH    = everything else
Flagged positions (scripts/flags.py DATA RULE) are excluded unless --include-flagged. Positions opened under a
leader profile are excluded from the numbers (they run other rules; the profile is shown in the last column).
"""
import argparse
import json
import os
import sqlite3
import statistics
import time
from collections import defaultdict

from scripts.flags import flagged_note, flagged_positions

LAMPORTS = 1e9


def med(xs):
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else None


def open_db(path):
    db = sqlite3.connect(f"file:{os.path.abspath(os.path.expanduser(path))}?mode=ro", uri=True, timeout=10)
    db.execute("PRAGMA busy_timeout=10000")
    return db


def signal_gap(db, token, ts, quote_sol, token_amount, within_s=10.0):
    """Gap of the first pool tick within `within_s` after his signal vs his price (lamports per raw)."""
    try:
        his = float(quote_sol) * LAMPORTS / int(token_amount)
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    r = db.execute("SELECT 1.0*sol_res/tok_res FROM ticks WHERE token=? AND ts>=? AND ts<=? AND tok_res>0 "
                   "ORDER BY ts LIMIT 1", (token, ts, ts + within_s)).fetchone()
    return 100 * (r[0] / his - 1) if r and r[0] and his > 0 else None


def position_gap(db, p):
    row = db.execute("SELECT quote_amount, token_amount FROM signals WHERE leader=? AND token=? AND side='buy' "
                     "AND action='bought' AND ABS(ts-?)<60 ORDER BY ABS(ts-?) LIMIT 1",
                     (p["leader"], p["token"], p["opened_at"], p["opened_at"])).fetchone()
    try:
        his = float(row[0]) / int(row[1])
        ours = float(p["cost"]) / int(p["token_amount"])
        return 100 * (ours / his - 1) if his > 0 else None
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def collect(db, from_id=0, sol_usd=200.0, flagged=None, days=14.0, now=None):
    now = now or time.time()
    flagged = flagged or {}
    cols = [r[1] for r in db.execute("PRAGMA table_info(positions)")]
    per = defaultdict(lambda: {"entries": 0, "gaps": [], "pnl_sol": [], "pnl_usd": [], "pnl_c": [], "pct": [],
                               "pool": 0.0, "jup": 0.0, "profile": None, "last": None, "open": 0})
    for r in db.execute("SELECT * FROM positions WHERE paper=1 AND id>=? ORDER BY id", (from_id,)):
        p = dict(zip(cols, r))
        if p["id"] in flagged:
            continue
        t = json.loads(p.get("timing") or "{}")
        s = per[p["leader"]]
        if t.get("profile"):
            s["profile"] = t["profile"]
            continue
        sol = p["chain"] == "solana"
        s["entries"] += 1
        if sol:
            s["gaps"].append(position_gap(db, p))
        else:
            gaps = [g for (g,) in db.execute("SELECT gap_pct FROM xchain_fills WHERE position_id=? AND side='buy' "
                                             "AND reason='open'", (p["id"],))]
            s["gaps"].append(med(gaps))
        if p["status"] != "closed" or p["exit_value"] is None:
            s["open"] += 1
            continue
        cost = float(p["cost"] or 0)
        pnl = float(p["exit_value"]) - cost
        (s["pnl_sol"] if sol else s["pnl_usd"]).append(pnl)
        s["pnl_c"].append(pnl * sol_usd if sol else pnl)
        s["pct"].append(100 * pnl / cost if cost else 0.0)
        if sol:
            model = (t.get("fill_model") or "").split("+")[0]
            if model == "jupiter":
                s["jup"] += pnl
            elif model:
                s["pool"] += pnl
    # buy signals: last activity (any chain), and entries/gaps for leaders without positions (watch leaders)
    for leader, token, ts, qa, ta, action in db.execute(
            "SELECT leader, token, ts, quote_amount, token_amount, action FROM signals WHERE side='buy' ORDER BY ts"):
        s = per[leader]
        s["last"] = ts if s["last"] is None else max(s["last"], ts)
        if action == "skip" and token and not token.startswith("0x"):
            g = signal_gap(db, token, ts, qa, ta)
            if g is not None:
                s["entries"] += 1
                s["gaps"].append(g)
    out = {}
    for leader, s in per.items():
        gaps = [g for g in s["gaps"] if g is not None]
        pnl_c = s["pnl_c"]
        closed = len(pnl_c)
        wo_best = sum(pnl_c) - max(pnl_c) if closed >= 2 else None
        wo_best2 = sum(pnl_c) - sum(sorted(pnl_c)[-2:]) if closed >= 3 else None
        avg = sum(s["pct"]) / closed if closed else None
        gm = med(gaps)
        last_days = (now - s["last"]) / 86400 if s["last"] else None
        rule = None
        if gm is not None and gm > 20 and len(gaps) >= 5:
            rule = f"CUT: median gap {gm:+.0f}% over {len(gaps)} entries"
        elif closed >= 15 and wo_best is not None and wo_best < 0 and avg is not None and avg < -5:
            rule = f"CUT: {closed} closed, w/o best ${wo_best:+,.0f}, avg {avg:+.1f}%"
        elif last_days is None or last_days > days:
            rule = f"CUT: no buy signal in {days:g} days" + (f" (last {last_days:.0f} d ago)" if last_days else "")
        elif closed >= 30 and wo_best2 is not None and wo_best2 > 0 and gm is not None and gm < 5:
            rule = f"PROMOTE: {closed} closed, w/o best 2 ${wo_best2:+,.0f}, median gap {gm:+.0f}%"
        else:
            rule = "WATCH"
        out[leader] = {"entries": s["entries"], "gap": gm, "gap_hi": (100 * sum(g > 20 for g in gaps) / len(gaps)) if gaps else None,
                       "closed": closed, "pnl_sol": sum(s["pnl_sol"]) if s["pnl_sol"] else None,
                       "pnl_usd": sum(s["pnl_usd"]) if s["pnl_usd"] else None, "wo_best": wo_best, "wo_best2": wo_best2,
                       "avg": avg, "wins": sum(x > 0 for x in pnl_c), "pool": s["pool"], "jup": s["jup"],
                       "last_days": last_days, "open": s["open"], "profile": s["profile"],
                       "label": rule.split(":")[0], "rule": rule}
    return out


def f(x, spec, unit=""):
    return "-" if x is None else f"{x:{spec}}{unit}"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default="copybot.db")
    ap.add_argument("--from-id", type=int, default=0)
    ap.add_argument("--sol-usd", type=float, default=200.0, help="SOL price for the combined USD PnL (default 200)")
    ap.add_argument("--days", type=float, default=14.0, help="CUT if no buy signal for this many days")
    ap.add_argument("--include-flagged", action="store_true")
    a = ap.parse_args()
    db = open_db(a.db)
    flagged = flagged_positions(db)
    print(flagged_note(flagged, a.include_flagged))
    stats = collect(db, a.from_id, a.sol_usd, None if a.include_flagged else flagged, a.days)
    print(f"LEADER SELECTION - paper, Solana + xchain, SOL at ${a.sol_usd:,.0f}; profile positions not counted\n")
    print(f"{'leader':<9} {'label':<8} {'entr':>4} {'gap med':>8} {'gap>20%':>7} {'closed':>6} {'open':>4} {'wins':>4} "
          f"{'PnL SOL':>8} {'PnL USD':>8} {'w/o best':>9} {'w/o b2':>8} {'avg %':>6} {'pool':>7} {'jup':>7} {'last':>5}  rule")
    order = sorted(stats, key=lambda l: ({"CUT": 2, "WATCH": 1, "PROMOTE": 0}[stats[l]["label"]], -stats[l]["closed"], l))
    for leader in order:
        s = stats[leader]
        print(f"{leader[:8]:<9} {s['label']:<8} {s['entries']:>4} {f(s['gap'], '+.0f', '%'):>8} {f(s['gap_hi'], '.0f', '%'):>7} "
              f"{s['closed']:>6} {s['open']:>4} {s['wins']:>4} {f(s['pnl_sol'], '+.2f'):>8} {f(s['pnl_usd'], '+,.0f'):>8} "
              f"{f(s['wo_best'], '+,.0f'):>9} {f(s['wo_best2'], '+,.0f'):>8} {f(s['avg'], '+.1f'):>6} "
              f"{f(s['pool'], '+.2f'):>7} {f(s['jup'], '+.2f'):>7} {f(s['last_days'], '.0f', 'd'):>5}  "
              f"{s['rule']}{f'  [profile {s['profile']}]' if s['profile'] else ''}")
    n = {k: sum(s["label"] == k for s in stats.values()) for k in ("PROMOTE", "WATCH", "CUT")}
    print(f"\n{len(stats)} leaders: {n['PROMOTE']} PROMOTE, {n['WATCH']} WATCH, {n['CUT']} CUT  "
          f"(w/o best in USD; pool / jup = Solana PnL in SOL by pricing class; last = days since his last buy signal)")


if __name__ == "__main__":
    main()
