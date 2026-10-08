"""Missed runners (read-only): what did the price do AFTER we exited each closed Solana paper position?

    python -m scripts.missed_runners [--db copybot.db] [--from-id N] [--no-gecko] [--limit 40] [--include-flagged]

Per closed position: peak after our exit vs our entry price and vs our exit price within 1 h / 24 h / 7 d,
time from exit to the 7-day peak, and the price now (vs entry). Prices come from our own pool ticks when
they cover the window (ticks_after_close_h after the close, raw or 1-minute bars), otherwise from
GeckoTerminal candles fetched on demand (never stored; ~2 s between calls for the free rate limit).
Price ratios use pool mid prices (first tick at/after the fill, last tick at/before the close), so fees and
our own impact are not in them. Sorted by peak-after-exit / exit price. RUNNER = reached >= 10x our entry
after we left. The per-leader summary counts those "runners we exited early".
Positions flagged by the DATA RULE (scripts/flags.py) are left out unless --include-flagged.
"""
import argparse
import json
import os
import sqlite3
import time
import urllib.request
from collections import defaultdict

from scripts.flags import flagged_positions

WINDOWS = (("1h", 3600), ("24h", 86400), ("7d", 7 * 86400))
GECKO = "https://api.geckoterminal.com/api/v2"


def price_path(db, token, t0, t1):
    """[(ts, price)] from raw ticks (sol_res / tok_res) or 1-minute bars (open, high, low, close) in [t0, t1]."""
    rows = db.execute("SELECT ts, 1.0*sol_res/tok_res FROM ticks WHERE token=? AND ts>=? AND ts<=? AND tok_res>0 "
                      "ORDER BY ts", (token, t0, t1)).fetchall()
    if rows:
        return rows
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='ticks_1m'").fetchone():
        return []
    out = []
    for ts, to, so, th, sh, tl, sl, tc, sc in db.execute(
            "SELECT ts, tok_open, sol_open, tok_high, sol_high, tok_low, sol_low, tok_close, sol_close FROM ticks_1m "
            "WHERE token=? AND ts>=? AND ts<=? ORDER BY ts", (token, t0 - 60, t1)):
        out += [(ts, so / to), (ts + 20, sl / tl), (ts + 40, sh / th), (ts + 59, sc / tc)]
    return out


def after_exit(path, fill_ts, close_ts):
    """-> dict with the ratios, or None if the path does not reach the close."""
    at_fill = next((p for ts, p in path if ts >= fill_ts), None)
    before_close = [p for ts, p in path if ts <= close_ts]
    later = [(ts, p) for ts, p in path if ts > close_ts]
    if at_fill is None or not before_close or not later:
        return None
    p_exit = before_close[-1]
    out = {"entry": at_fill, "exit": p_exit, "covered_s": later[-1][0] - close_ts}
    for name, span in WINDOWS:
        win = [(ts, p) for ts, p in later if ts <= close_ts + span]
        if win:
            t_peak, peak = max(win, key=lambda x: x[1])
            out[name] = (peak / at_fill, peak / p_exit, t_peak - close_ts)
    return out


class Gecko:
    """GeckoTerminal, on demand, rate-limited; prices in the pool's quote token (SOL) when possible."""
    def __init__(self, pause=2.1):
        self.pause, self.last, self.calls = pause, 0.0, 0

    def get(self, path):
        time.sleep(max(0.0, self.last + self.pause - time.time()))
        self.last = time.time()
        self.calls += 1
        req = urllib.request.Request(GECKO + path, headers={"Accept": "application/json", "User-Agent": "copybot-research"})
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.load(r)

    def pool(self, token):
        pools = self.get(f"/networks/solana/tokens/{token}/pools?page=1").get("data") or []
        if not pools:
            return None, None
        a = pools[0]["attributes"]
        now = a.get("base_token_price_quote_token")
        return pools[0]["id"].split("_", 1)[-1], (float(now) if now else None)

    def candles(self, pool, timeframe, aggregate, before, limit):
        j = self.get(f"/networks/solana/pools/{pool}/ohlcv/{timeframe}?aggregate={aggregate}&before_timestamp="
                     f"{int(before)}&limit={limit}&currency=token")
        return [(c[0], c[2]) for c in (j.get("data") or {}).get("attributes", {}).get("ohlcv_list") or []]  # (ts, high)

    def path(self, token, fill_ts, close_ts):
        """Minute highs for the first hour after the close, hourly highs for 7 days, plus the fill/close prices
        (minute candles around them). -> ([(ts, price)], price_now)."""
        pool, now = self.pool(token)
        if not pool:
            return [], None
        pts = self.candles(pool, "minute", 1, close_ts + 3600, 90)            # covers ~30 min before the close too
        pts += self.candles(pool, "hour", 1, close_ts + 7 * 86400 + 3600, 170)
        pts += self.candles(pool, "minute", 1, fill_ts + 600, 15)
        return sorted(set(pts)), now


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default="copybot.db")
    ap.add_argument("--from-id", type=int, default=0)
    ap.add_argument("--no-gecko", action="store_true", help="our ticks only (no network)")
    ap.add_argument("--limit", type=int, default=40, help="rows to print")
    ap.add_argument("--include-flagged", action="store_true")
    a = ap.parse_args()
    db = sqlite3.connect(f"file:{os.path.abspath(os.path.expanduser(a.db))}?mode=ro", uri=True)
    db.execute("PRAGMA busy_timeout=10000")
    flagged = {} if a.include_flagged else flagged_positions(db)
    gecko = None if a.no_gecko else Gecko()
    rows, per_leader = [], defaultdict(lambda: {"n": 0, "runners": 0, "x2_exit": 0, "no_data": 0})
    for pid, token, leader, cost, tokens, exitv, opened, closed, reason, timing in db.execute(
            "SELECT id, token, leader, cost, token_amount, exit_value, opened_at, closed_at, exit_reason, timing "
            "FROM positions WHERE paper=1 AND chain='solana' AND status='closed' AND id>=? ORDER BY id", (a.from_id,)):
        if pid in flagged:
            continue
        t = json.loads(timing or "{}")
        fill_ts = t.get("filled_at") or opened
        path = price_path(db, token, opened - 5, closed + 7 * 86400)
        r, src, now = after_exit(path, fill_ts, closed), "ticks", None
        if (r is None or r["covered_s"] < 3600) and gecko is not None and time.time() - closed > 600:
            try:
                gpath, now = gecko.path(token, fill_ts, closed)
                g = after_exit(gpath, fill_ts, closed)
                if g is not None and (r is None or g["covered_s"] > r["covered_s"]):
                    r, src = g, "gecko"
            except Exception as e:
                print(f"  #{pid}: GeckoTerminal failed ({type(e).__name__})")
        s = per_leader[leader]
        s["n"] += 1
        if r is None:
            s["no_data"] += 1
            continue
        best = r.get("7d") or r.get("24h") or r.get("1h")
        runner = best[0] >= 10
        s["runners"] += runner
        s["x2_exit"] += best[1] >= 2
        rows.append((best[1], pid, leader, token, (exitv - cost) / cost, reason, r, src, now, runner))
    rows.sort(key=lambda x: -x[0])
    print(f"MISSED RUNNERS - {len(rows)} closed Solana paper positions with a price path after the exit"
          f"{'' if a.include_flagged else f', {len(flagged)} flagged left out'}"
          f"{'' if gecko is None else f', GeckoTerminal calls {gecko.calls}'}")
    print("peak after exit as a multiple of our ENTRY price / our EXIT price, within 1 h, 24 h, 7 d; "
          "t = exit -> 7 d peak; now = price now / entry\n")
    hdr = (f"{'id':>5} {'leader':<9} {'token':<10} {'pnl':>7} {'exit':<13} {'1h e/x':>12} {'24h e/x':>12} "
           f"{'7d e/x':>12} {'t peak':>7} {'now':>6} {'src':<6} flag")
    print(hdr)
    for _, pid, leader, token, pnl, reason, r, src, now, runner in rows[:a.limit]:
        def cell(name):
            v = r.get(name)
            return f"{v[0]:>5.1f}x/{v[1]:>4.1f}x" if v else "-".rjust(12)
        best = r.get("7d") or r.get("24h") or r.get("1h")
        print(f"{pid:>5} {leader[:8]:<9} {token[:10]:<10} {100 * pnl:>+6.0f}% {(reason or '')[:13]:<13} {cell('1h'):>12} "
              f"{cell('24h'):>12} {cell('7d'):>12} {best[2] / 3600:>6.1f}h {(f'{now / r['entry']:.1f}x' if now else '-'):>6} "
              f"{src:<6} {'RUNNER' if runner else ''}")
    print("\nPER LEADER   runners = reached >= 10x our entry after we left; x2 exit = reached >= 2x our exit price")
    print(f"{'leader':<9} {'closed':>6} {'runners':>7} {'x2 exit':>7} {'no data':>7}")
    for leader, s in sorted(per_leader.items(), key=lambda kv: (-kv[1]["runners"], -kv[1]["x2_exit"], kv[0])):
        print(f"{leader[:8]:<9} {s['n']:>6} {s['runners']:>7} {s['x2_exit']:>7} {s['no_data']:>7}")
    print("\n(entry/exit = pool mid prices around our fill and close; GeckoTerminal candle highs where our ticks "
          "end early; nothing is stored)")


if __name__ == "__main__":
    main()
