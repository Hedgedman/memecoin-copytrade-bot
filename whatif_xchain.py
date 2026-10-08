"""What-if for cross-chain PAPER positions (robinhood / bsc), on the bot's own data. Read-only.

    python whatif_xchain.py [--db copybot.db] [--from-id N] [--leader PREFIX] [--chain bsc] [--profile NAME] [--list]

Profiles / rules: positions opened under a leader profile (bot/profiles.py) carry timing["profile"]; normal
leaders since Oct 8 2026 carry timing["exits"]["rule"] = "H6" (TP +30% / stop -20% / max hold, his sells
ignored); older ones are "default" (his sells mirrored + safety stop). --profile default / H6 / trail_Leader-B
selects one label; the per-leader table splits by it. Trail / H6 positions do not mirror his sells: their
own trailing_stop / stop_loss / take_profit / max_hold sells are replayed as price points (like safety_stop),
so for those positions "actual" is the result that matters, not the mirror replay.

Entry-gap filter (report only): what if we had skipped a copy when our entry price was already more than
X% above his (X = 10/20/40%)? Decided on the opening fill; add-ons of kept positions stay as they were.
Per leader (always shown): positions, actual PnL, mirror/-60% replay, entry gap (our fill vs his price),
and how late we saw (seen) and mirrored (filled) his sells, from xchain_fills.

Unlike whatif_ticksv2.py (Solana, every pool update) these positions only have a Relay quote per
mark (~60 s) and per fill, so stops are checked at mark resolution and filled at the mark's price.
Exit styles, each with safety stops none / -40 / -50 / -60 / -70 %:
  mirror     sell the same fraction he sells, at our recorded fill price (what the bot does)
  first_sell sell everything at his FIRST sell
  hold       ignore his sells; only the stop (or the data running out) ends the trade
Data ends when the real position closed: a variant still holding then is valued at the last price
and counted as censored (c). The real bot uses a -60% stop, so looser stops are mostly censored.
Amounts in USD. Buys (incl. add-ons) are the same as the real ones in every variant.
"""
import argparse
import json
import os
import sqlite3

STOPS = (None, 0.40, 0.50, 0.60, 0.70)
STYLES = ("mirror", "first_sell", "hold")
OWN_EXITS = ("safety_stop", "trailing_stop", "stop_loss", "take_profit", "max_hold")   # our own sells, not his


def rule_of(timing: str | None) -> str:
    t = json.loads(timing or "{}")
    return t.get("profile") or (t.get("exits") or {}).get("rule") or "default"


def load(db, from_id, leader="", chain="", profile=""):
    """-> [(pid, chain, cost, exit_value, status, events, leader, profile)]"""
    out = []
    for pid, chain, cost, exitv, status, timing, who in db.execute(
            "SELECT id, chain, cost, exit_value, status, timing, leader FROM positions WHERE paper=1 "
            "AND chain!='solana' AND id>=? AND leader LIKE ? AND chain LIKE ? ORDER BY id",
            (from_id, leader + "%", chain or "%")):
        prof = rule_of(timing)
        if profile and prof != profile:
            continue
        rows = db.execute("SELECT ts, side, fraction, our_usd, our_tokens, reason FROM xchain_fills "
                          "WHERE position_id=? ORDER BY ts", (pid,)).fetchall()
        if not rows:
            continue
        fills = [r[:5] for r in rows]
        # our own exits (safety stop, profile trail/stop) are not his trades: replay them only as a price point
        ev = [(ts, "mark", None, None, usd / int(tok), None) if any(x in (reason or "") for x in OWN_EXITS) else
              (ts, "fill", side, frac, usd, int(tok)) for ts, side, frac, usd, tok, reason in rows]
        # marks hold realized + value of the remaining tokens: rebuild the per-token price from them
        realized, remaining, k = 0.0, 0, 0
        marks = db.execute("SELECT ts, value FROM marks WHERE position_id=? ORDER BY ts", (pid,)).fetchall()
        for ts, value in marks:
            while k < len(fills) and fills[k][0] <= ts:
                _, side, _, usd, tok = fills[k]
                realized, remaining = (realized, remaining + int(tok)) if side == "buy" else \
                    (realized + usd, remaining - int(tok))
                k += 1
            if remaining > 0:
                ev.append((ts, "mark", None, None, (value - realized) / remaining, None))
        ev.sort(key=lambda e: (e[0], e[1] != "fill"))
        out.append((pid, chain, cost, exitv, status, ev, who, prof))
    return out


def closed_pnl(data):
    return sum(d[3] - d[2] for d in data if d[4] == "closed")


def avg(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def fmt(x, spec, unit=""):
    return "-" if x is None else f"{x:{spec}}{unit}"


GAP_LIMITS = (None, 10.0, 20.0, 40.0)


def gap_filter(db, data):
    gaps = {pid: g for pid, g in db.execute("SELECT position_id, gap_pct FROM xchain_fills WHERE side='buy' "
                                             "AND reason='open'")}
    print("ENTRY-GAP FILTER (report only): skip the copy if our opening fill was > X% above his price")
    print(f"{'skip if gap >':<14} {'kept':>4} {'mirror-60 $':>12} {'wins':>7} {'actual closed $':>15} "
          f"{'skipped':>7} {'their mirror-60 $':>17}")
    for lim in GAP_LIMITS:
        keep = [d for d in data if lim is None or gaps.get(d[0]) is None or gaps[d[0]] <= lim]
        skip = [d for d in data if d not in keep]
        res = [simulate(d[5], "mirror", 0.60) for d in keep]
        sres = [simulate(d[5], "mirror", 0.60) for d in skip]
        actual = closed_pnl(keep)
        print(f"{'no filter' if lim is None else f'{lim:.0f}%':<14} {len(keep):>4} {sum(r[0] for r in res):>+12,.0f} "
              f"{sum(r[0] > 0 for r in res):>3}/{len(res):<3} {actual:>+15,.0f} {len(skip):>7} "
              f"{(f'{sum(r[0] for r in sres):+,.0f}' if sres else '-'):>17}")
    unknown = sum(gaps.get(d[0]) is None for d in data)
    if unknown:
        print(f"  {unknown} position(s) without his price (gap unknown) are always kept")


def by_leader(db, data):
    print(f"{'leader':<9} {'profile':<12} {'pos':>4} {'closed':>6} {'actual $':>10} {'mirror-60 $':>12} {'wins':>7} "
          f"{'entry gap':>9} {'sell gap':>8} {'sell seen':>9} {'sell filled':>11}")
    cols = {r[1] for r in db.execute("PRAGMA table_info(xchain_fills)")}
    groups = sorted({(d[6], d[7]) for d in data}, key=lambda g: -sum(1 for d in data if (d[6], d[7]) == g))
    for who, prof in groups:
        rows = [d for d in data if (d[6], d[7]) == (who, prof)]
        ids = tuple(d[0] for d in rows)
        seen = "detect_delay_s" if "detect_delay_s" in cols else "NULL"          # older DB: column not there yet
        q = f"SELECT side, gap_pct, {seen}, delay_s FROM xchain_fills WHERE position_id IN ({','.join('?' * len(ids))})"
        fills = db.execute(q, ids).fetchall()
        sells = [f for f in fills if f[0] == "sell"]
        res = [simulate(d[5], "mirror", 0.60) for d in rows]
        actual = closed_pnl(rows)
        print(f"{who[:8]:<9} {prof[:12]:<12} {len(rows):>4} {sum(d[4] == 'closed' for d in rows):>6} {actual:>+10,.0f} "
              f"{sum(r[0] for r in res):>+12,.0f} {sum(r[0] > 0 for r in res):>3}/{len(res):<3} "
              f"{fmt(avg([f[1] for f in fills if f[0] == 'buy']), '+.1f', '%'):>9} "
              f"{fmt(avg([f[1] for f in sells]), '+.1f', '%'):>8} {fmt(avg([f[2] for f in sells]), '.0f', 's'):>9} "
              f"{fmt(avg([f[3] for f in sells]), '.0f', 's'):>11}")


def simulate(ev, style, stop):
    """-> (pnl USD, censored)."""
    rem, basis, cost, cash, last_unit, sold_once = 0, 0.0, 0.0, 0.0, None, False
    for ts, kind, side, frac, a, b in ev:
        if kind == "fill" and side == "buy":
            rem, basis, cost = rem + b, basis + a, cost + a
            last_unit = a / b
        elif kind == "fill":                                   # his sell -> our recorded fill price
            unit = a / b
            last_unit = unit
            if rem <= 0 or style == "hold" or (style == "first_sell" and sold_once):
                continue
            n = rem if style == "first_sell" or frac >= 0.99 else int(rem * frac)
            cash += n * unit
            basis -= basis * n / rem
            rem -= n
            sold_once = True
        else:                                                  # mark: per-token value of the remaining
            last_unit = a
            if rem > 0 and stop is not None and rem * a <= (1 - stop) * basis:
                cash += rem * a
                rem, basis = 0, 0.0
    censored = rem > 0
    if censored and last_unit is not None:
        cash += rem * last_unit
    return cash - cost, censored


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default="copybot.db")
    ap.add_argument("--from-id", type=int, default=0)
    ap.add_argument("--leader", default="", help="only this leader (wallet prefix)")
    ap.add_argument("--chain", default="", help="only this chain (robinhood / bsc)")
    ap.add_argument("--profile", default="", help="only this profile / rule label: default, H6, trail_Leader-B, ...")
    ap.add_argument("--list", action="store_true", help="one line per position (mirror, -60%%)")
    args = ap.parse_args()
    db = sqlite3.connect(f"file:{os.path.abspath(os.path.expanduser(args.db))}?mode=ro", uri=True)
    data = load(db, args.from_id, args.leader, args.chain, args.profile)
    if not data:
        print("no xchain paper positions with fills yet")
        return
    actual = closed_pnl(data)
    n_closed = sum(d[4] == "closed" for d in data)
    print(f"{len(data)} xchain paper positions ({n_closed} closed, actual closed PnL ${actual:+,.2f})")
    print("cells: PnL USD  wins/n  c = censored (still holding when the data ended)\n")
    print(f"{'style':<11}" + "".join(f"{'stop ' + (f'-{s:.0%}' if s else 'none'):>24}" for s in STOPS))
    for style in STYLES:
        cells = []
        for stop in STOPS:
            res = [simulate(d[5], style, stop) for d in data]
            pnl, wins, cens = sum(r[0] for r in res), sum(r[0] > 0 for r in res), sum(r[1] for r in res)
            cells.append(f"{pnl:+10,.0f} {wins:>3}/{len(res):<3} c{cens:<3}")
        print(f"{style:<11}" + "".join(f"{c:>24}" for c in cells))
    print()
    gap_filter(db, data)
    print()
    by_leader(db, data)
    if args.list:
        print()
        for pid, chain, cost, exitv, status, ev, who, prof in data:
            pnl, c = simulate(ev, "mirror", 0.60)
            print(f"  #{pid:<5} {who[:8]:<9} {prof[:12]:<12} {chain:<10} cost ${cost:>8,.2f}  {status:<6} actual "
                  f"{'' if exitv is None else f'${exitv - cost:+,.2f}':>10}  sim ${pnl:+,.2f}{' (c)' if c else ''}")


if __name__ == "__main__":
    main()
