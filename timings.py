"""Where does the time go? Per-stage latency of every position (needs positions opened
by the new version; old positions have no timing data).

Usage (from the copybot folder):
    python timings.py                # all positions
    python timings.py --from-id 14   # only newer positions

Stages
  fetch      websocket notification -> leader tx downloaded and parsed (getTransaction)
  checks     decision -> safety checks done (pool read or Jupiter quotes)
  fill       checks done -> filled. PAPER: this is mostly paper_land_delay_s (simulated).
  total      notification -> filled
  slots      slots between the leader's tx and the pool state we filled on (paper) or
             our landed tx (live). THE number to push down. 1 slot ~ 0.4 s.
  exit lag   stop/TP triggered -> exit filled
  exit slip  exit value vs value at the trigger: what the delay cost on the way out
  entry gap  first exit quote vs cost, right after buying: entry costs as the bot saw them
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import statistics


def pct(xs, q):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * (len(xs) - 1) + 0.5))]


def line(name, xs, fmt="{:.2f}s"):
    if not xs:
        print(f"  {name:<11} no data")
        return
    print(f"  {name:<11} median {fmt.format(statistics.median(xs)):>9}   p90 {fmt.format(pct(xs, .9)):>9}"
          f"   max {fmt.format(max(xs)):>9}   n={len(xs)}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="copybot.db")
    ap.add_argument("--from-id", type=int, default=1)
    args = ap.parse_args()
    db = sqlite3.connect(args.db)
    rows = db.execute("SELECT id, cost, ref_value, exit_value, timing, paper FROM positions WHERE id>=? AND chain='solana' "
                      "ORDER BY id", (args.from_id,)).fetchall()
    groups: dict[str, dict[str, list]] = {}
    for pid, cost, ref, exit_value, timing, paper in rows:
        t = json.loads(timing) if timing else {}
        model = (t.get("fill_model") or "old (no timing)") if t else "old (no timing)"
        g = groups.setdefault(model, {k: [] for k in
                                      ("fetch", "checks", "fill", "total", "slots", "exit lag", "exit slip", "entry gap")})
        if ref and cost:
            g["entry gap"].append(ref / cost - 1)
        if not t:
            continue
        n, f, d, c, fl = (t.get(k) for k in ("notified_at", "fetched_at", "decided_at", "checked_at", "filled_at"))
        if n and f: g["fetch"].append(f - n)
        if d and c: g["checks"].append(c - d)
        if c and fl: g["fill"].append(fl - c)
        if n and fl: g["total"].append(fl - n)
        if t.get("fill_slot") and t.get("leader_slot"):
            g["slots"].append(t["fill_slot"] - t["leader_slot"])
        if t.get("exit_trigger_at") and t.get("exit_filled_at"):
            g["exit lag"].append(t["exit_filled_at"] - t["exit_trigger_at"])
        if t.get("exit_trigger_value") and exit_value is not None:
            g["exit slip"].append(exit_value / t["exit_trigger_value"] - 1)
    if not groups:
        print("No positions yet.")
        return
    for model, g in groups.items():
        print(f"\n[{model}]")
        for k in ("fetch", "checks", "fill", "total"):
            line(k, g[k])
        line("slots", g["slots"], "{:.0f}")
        line("exit lag", g["exit lag"])
        line("exit slip", g["exit slip"], "{:+.1%}")
        line("entry gap", g["entry gap"], "{:+.1%}")
    print("\nPaper 'fill' is mostly the simulated delay. Calibrate paper_land_delay_s from tiny LIVE trades.")


if __name__ == "__main__":
    main()
