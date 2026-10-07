"""Daily cross-chain summary, per leader and chain (read-only on copybot.db).

Usage (from the copybot folder):
    python -m scripts.xchain_report [--hours 24] [--all] [--db copybot.db]

His trades (Relay)  every leader's cross-chain buys/sells in the window, all chains (watch-only ones too):
                    median size, how late we saw his sells (detect_delay_s), and the price ladder at +16 s
                    (Relay /price for HIS size after we detected his trade, vs his price; buy + = dearer).
Our paper           robinhood/bsc paper copies: buys copied / skipped, entry gap (our fill vs his price),
                    sell gap (our mirrored sell vs his), closed positions with mirrored exits (PnL USD),
                    open positions (value = realized + latest Relay quote for the rest, minus cost).
Medians, not means: a few memecoin entries 40-70% above his price would swamp an average.
Sample sizes are printed on purpose: a row with 1-2 trades says nothing.
"""
import argparse
import json
import os
import sqlite3
import statistics
import time
from collections import defaultdict
from datetime import datetime


def med(xs):
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else None


def f(x, spec, unit=""):
    return "-" if x is None else f"{x:{spec}}{unit}"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default="copybot.db")
    ap.add_argument("--hours", type=float, default=24.0)
    ap.add_argument("--all", action="store_true", help="whole history instead of the last --hours")
    a = ap.parse_args()
    db = sqlite3.connect(f"file:{os.path.abspath(os.path.expanduser(a.db))}?mode=ro", uri=True)
    since = 0.0 if a.all else time.time() - a.hours * 3600
    has_col = lambda t, c: c in {r[1] for r in db.execute(f"PRAGMA table_info({t})")}
    has_tab = lambda t: bool(db.execute("SELECT 1 FROM sqlite_master WHERE name=?", (t,)).fetchone())
    if not has_tab("xchain_trades"):
        print("no xchain data in this database")
        return
    seen_col = "detect_delay_s" if has_col("xchain_trades", "detect_delay_s") else "NULL"
    when = "all history" if a.all else f"last {a.hours:g} h (since {datetime.fromtimestamp(since):%m-%d %H:%M})"
    print(f"CROSS-CHAIN REPORT - {when}\n")

    # ---------- his trades ----------
    his = defaultdict(lambda: {"buy": [], "sell": [], "seen": [], "lad_buy": [], "lad_sell": []})
    for leader, chain, side, usd, seen in db.execute(
            f"SELECT leader, chain, side, usd, {seen_col} FROM xchain_trades WHERE status='success' "
            "AND side IN ('buy','sell') AND COALESCE(deposit_ts, ts) >= ?", (since,)):
        h = his[(leader, chain)]
        h[side].append(usd)
        if side == "sell":
            h["seen"].append(seen)
    if has_tab("xchain_prices"):
        for leader, chain, side, gap in db.execute(
                "SELECT t.leader, t.chain, t.side, p.gap_pct FROM xchain_prices p JOIN xchain_trades t "
                "ON t.request_id = p.request_id WHERE p.offset_s = (SELECT MAX(offset_s) FROM xchain_prices) "
                "AND COALESCE(t.deposit_ts, t.ts) >= ?", (since,)):
            his[(leader, chain)]["lad_" + side].append(gap)
    print("HIS TRADES (Relay)                                   ladder = Relay price for his size at the last "
          "offset after detection")
    print(f"{'leader':<9} {'chain':<11} {'buys':>4} {'sells':>5} {'med buy $':>9} {'sell seen':>9} "
          f"{'ladder buy':>10} {'ladder sell':>11}")
    keys = sorted(his, key=lambda k: (-(len(his[k]["buy"]) + len(his[k]["sell"])), k))
    for leader, chain in keys:
        h = his[(leader, chain)]
        print(f"{leader[:8]:<9} {chain:<11} {len(h['buy']):>4} {len(h['sell']):>5} {f(med(h['buy']), ',.0f'):>9} "
              f"{f(med(h['seen']), '.0f', 's'):>9} {f(med(h['lad_buy']), '+.1f', '%'):>10} "
              f"{f(med(h['lad_sell']), '+.1f', '%'):>11}")
    if not keys:
        print("  no Relay trades in this window")
    nb = sum(len(h["buy"]) for h in his.values())
    ns = sum(len(h["sell"]) for h in his.values())
    print(f"{'total':<21} {nb:>4} {ns:>5}\n")

    # ---------- our paper ----------
    ours = defaultdict(lambda: {"copied": 0, "skipped": 0, "entry": [], "sellgap": [], "closed": [], "open": []})
    leader_col = "COALESCE(f.leader, p.leader)" if has_col("xchain_fills", "leader") else "p.leader"
    for leader, chain, side, gap in db.execute(
            f"SELECT {leader_col}, f.chain, f.side, f.gap_pct FROM xchain_fills f JOIN positions p "
            "ON p.id = f.position_id WHERE f.ts >= ?", (since,)):
        o = ours[(leader, chain)]
        if side == "buy":
            o["copied"] += 1
            o["entry"].append(gap)
        else:
            o["sellgap"].append(gap)
    for leader, detail in db.execute("SELECT leader, detail FROM leader_events WHERE kind='xchain_paper_skip' "
                                     "AND ts >= ?", (since,)):
        ours[(leader or "?", (detail or "?").split(" ")[0])]["skipped"] += 1
    open_rows = []
    for pid, leader, chain, cost, exitv, status, closed_at, opened, timing in db.execute(
            "SELECT id, leader, chain, cost, exit_value, status, closed_at, opened_at, timing FROM positions "
            "WHERE paper=1 AND chain!='solana' AND (status!='closed' OR closed_at >= ?)", (since,)):
        o = ours[(leader, chain)]
        if status == "closed":
            o["closed"].append(exitv - cost)
        else:
            mark = db.execute("SELECT value FROM marks WHERE position_id=? ORDER BY ts DESC LIMIT 1", (pid,)).fetchone()
            pnl = mark[0] - cost if mark else None
            o["open"].append(pnl)
            open_rows.append((pid, leader, chain, json.loads(timing or "{}"), cost, pnl, opened))
    print("OUR PAPER (robinhood/bsc)        gaps: + = we paid more (buy) / got more (sell) than him")
    print(f"{'leader':<9} {'chain':<11} {'copied':>6} {'skipped':>7} {'entry gap':>9} {'sell gap':>8} "
          f"{'closed':>6} {'won':>4} {'closed PnL $':>12} {'open':>4} {'open PnL $':>10}")
    tot = defaultdict(float)
    for (leader, chain), o in sorted(ours.items(), key=lambda kv: (-kv[1]["copied"] - len(kv[1]["open"]), kv[0])):
        cl, op = o["closed"], [x for x in o["open"] if x is not None]
        print(f"{leader[:8]:<9} {chain:<11} {o['copied']:>6} {o['skipped']:>7} {f(med(o['entry']), '+.1f', '%'):>9} "
              f"{f(med(o['sellgap']), '+.1f', '%'):>8} {len(cl):>6} {sum(x > 0 for x in cl):>4} "
              f"{(f'{sum(cl):+,.0f}' if cl else '-'):>12} {len(o['open']):>4} {(f'{sum(op):+,.0f}' if op else '-'):>10}")
        tot["copied"] += o["copied"]; tot["skipped"] += o["skipped"]; tot["closed"] += len(cl)
        tot["pnl"] += sum(cl); tot["open"] += len(o["open"]); tot["open_pnl"] += sum(op)
    if not ours:
        print("  no paper activity in this window")
    print(f"{'total':<21} {tot['copied']:>6.0f} {tot['skipped']:>7.0f} {'':>9} {'':>8} {tot['closed']:>6.0f} {'':>4} "
          f"{tot['pnl']:>+12,.0f} {tot['open']:>4.0f} {tot['open_pnl']:>+10,.0f}")
    if tot["closed"] < 20:
        print(f"  note: {tot['closed']:.0f} closed positions - far too few to judge a result (aim for 30+ per leader)")

    if open_rows:
        print("\nOPEN POSITIONS")
        for pid, leader, chain, t, cost, pnl, opened in sorted(open_rows):
            age = (time.time() - opened) / 3600
            print(f"  #{pid:<5} {leader[:8]:<9} {chain:<10} {t.get('symbol', '')[:12]:<12} cost ${cost:>8,.2f}  "
                  f"now {f(pnl, '+,.2f', ' $'):>12} ({f(100 * pnl / cost if pnl is not None else None, '+.0f', '%')})"
                  f"  held {age:.1f} h")


if __name__ == "__main__":
    main()
