"""What-if for cross-chain PAPER positions (robinhood / bsc), on the bot's own data. Read-only.

    python whatif_xchain.py [--db copybot.db] [--from-id N] [--list]

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
import os
import sqlite3

STOPS = (None, 0.40, 0.50, 0.60, 0.70)
STYLES = ("mirror", "first_sell", "hold")


def load(db, from_id):
    out = []
    for pid, chain, cost, exitv, status, timing in db.execute(
            "SELECT id, chain, cost, exit_value, status, timing FROM positions WHERE paper=1 AND chain!='solana' "
            "AND id>=? ORDER BY id", (from_id,)):
        rows = db.execute("SELECT ts, side, fraction, our_usd, our_tokens, reason FROM xchain_fills "
                          "WHERE position_id=? ORDER BY ts", (pid,)).fetchall()
        if not rows:
            continue
        fills = [r[:5] for r in rows]
        # our own safety-stop sells are not his trades: replay them only as a price point
        ev = [(ts, "mark", None, None, usd / int(tok), None) if "safety_stop" in (reason or "") else
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
        out.append((pid, chain, cost, exitv, status, ev))
    return out


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
    ap.add_argument("--list", action="store_true", help="one line per position (mirror, -60%%)")
    args = ap.parse_args()
    db = sqlite3.connect(f"file:{os.path.abspath(os.path.expanduser(args.db))}?mode=ro", uri=True)
    data = load(db, args.from_id)
    if not data:
        print("no xchain paper positions with fills yet")
        return
    actual = sum(e - c for _, _, c, e, s, _ in data if s == "closed")
    n_closed = sum(s == "closed" for *_, s, _ in data)
    print(f"{len(data)} xchain paper positions ({n_closed} closed, actual closed PnL ${actual:+,.2f})")
    print("cells: PnL USD  wins/n  c = censored (still holding when the data ended)\n")
    print(f"{'style':<11}" + "".join(f"{'stop ' + (f'-{s:.0%}' if s else 'none'):>24}" for s in STOPS))
    for style in STYLES:
        cells = []
        for stop in STOPS:
            res = [simulate(ev, style, stop) for *_, ev in data]
            pnl, wins, cens = sum(r[0] for r in res), sum(r[0] > 0 for r in res), sum(r[1] for r in res)
            cells.append(f"{pnl:+10,.0f} {wins:>3}/{len(res):<3} c{cens:<3}")
        print(f"{style:<11}" + "".join(f"{c:>24}" for c in cells))
    if args.list:
        print()
        for pid, chain, cost, exitv, status, ev in data:
            pnl, c = simulate(ev, "mirror", 0.60)
            print(f"  #{pid:<5} {chain:<10} cost ${cost:>8,.2f}  {status:<6} actual "
                  f"{'' if exitv is None else f'${exitv - cost:+,.2f}':>10}  sim ${pnl:+,.2f}{' (c)' if c else ''}")


if __name__ == "__main__":
    main()
