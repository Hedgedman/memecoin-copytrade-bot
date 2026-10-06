"""What-if on TICK data: replay every pool update and compare exit styles.

Usage (from the copybot folder):
    python whatif_ticks.py --from-id 44              # newer positions only
    python whatif_ticks.py --from-id 44 --delay 0.5  # exits land 0.5 s after the trigger
    python whatif_ticks.py --from-id 44 --sl 0.15    # safety stop used by trailing/partial styles
    python whatif_ticks.py --from-id 44 --max-mc 500000              # only entries below $500k market cap
    python whatif_ticks.py --from-id 44 --min-mc 500000              # only entries at/above $500k

Exit styles compared
  fixed     sell everything at +TP, or at the stop                (what the bot does now)
  trailing  once up +A%, sell everything when value falls X% from its peak; stop before that
  partial   sell part at +TP1, the rest with a trailing stop X% from the peak; stop before TP1

How it works
- Prices come from the `ticks` table (every pool change), valued with exact pool math for
  selling what you hold (price impact + pool fee included).
- Exits trigger on a tick and FILL on the first tick >= trigger + delay.
- Paper never really bought, so every tick is valued on the pool WITH your buy in it (and
  minus anything you already sold): a "shadow pool".
- "censored" = the recorded data ended while the strategy still held. Its value at the last
  tick is used, which is a guess. Many censored trades = the number is unreliable.
  Raise tick_tail_s in config.yaml to record longer after each close.
"""
from __future__ import annotations

import argparse
import math
import sqlite3
from dataclasses import dataclass

import yaml

from bot.pricing.pools import PoolState

L = 1_000_000_000
FIXED_TPS = [0.10, 0.20, 0.30, 0.50, 1.00, 2.00, 4.00]          # +100% = 2x, +200% = 3x, +400% = 5x
FIXED_SLS = [0.05, 0.10, 0.15, 0.20, 0.30]
TRAIL_ACTS = [0.10, 0.20, 0.30, 0.50, 1.00]
TRAIL_PCTS = [0.10, 0.20, 0.30, 0.40]
PART_TP1 = [0.20, 0.30, 0.50, 1.00]
PART_TRAIL = [0.15, 0.25, 0.40]
PEAK_LEVELS = [0.30, 0.50, 1.00, 2.00, 4.00]


@dataclass
class Pos:
    pid: int
    cost: float          # SOL incl. paper buy fee
    tokens: int          # raw
    entry_net: int       # lamports that entered the pool from our buy (after pool fee)
    series: list         # [(ts, PoolState, fee_bps, factor)]  factor = real value / pool-model value
    factor: float = 1.0  # kept for compatibility


def shifted(st: PoolState, net_in: int) -> PoolState:
    """Observed pool + our net SOL position (constant product: state follows net SOL in)."""
    if net_in == 0:
        return st
    k = st.sol_res * st.tok_res
    sol2 = st.sol_res + net_in
    if sol2 <= 0:
        return st
    tok2 = -(-k // sol2)
    d_tok = st.tok_res - tok2
    return PoolState(st.venue, tok2, sol2,
                     None if st.real_tok is None else st.real_tok - d_tok,
                     None if st.real_sol is None else st.real_sol + net_in)


def sell(st: PoolState, fee: int, tokens: int, net_in: int) -> tuple[int, int]:
    """(lamports received after pool fee, gross lamports removed from the pool)."""
    out = shifted(st, net_in).sell_out(tokens, fee)
    gross = out * 10_000 // max(1, 10_000 - fee)
    return out, gross


def entry_mc(db, token, leader, opened):
    """Market cap at entry, from the 'mc $1,234,567' the bot writes in the bought signal's reason."""
    r = db.execute("SELECT reason FROM signals WHERE token=? AND leader=? AND action='bought' "
                   "ORDER BY ABS(ts - ?) LIMIT 1", (token, leader, opened)).fetchone()
    if not r or not r[0] or "mc $" not in r[0]:
        return None
    try:
        return float(r[0].split("mc $")[1].split()[0].replace(",", ""))
    except ValueError:
        return None


def load(db_path, from_id, fees, paper_fee, max_after_s, min_mc=None, max_mc=None, live=False):
    db = sqlite3.connect(db_path)
    rows = db.execute("SELECT id, token, token_amount, cost, opened_at, leader FROM positions WHERE paper=? AND "
                      "status='closed' AND id>=? ORDER BY id", (0 if live else 1, from_id)).fetchall()
    out, skipped, mc_out = [], 0, 0
    for pid, token, amount, cost, opened, leader in rows:
        if min_mc is not None or max_mc is not None:
            mc = entry_mc(db, token, leader, opened)
            if mc is None or (min_mc is not None and mc < min_mc) or (max_mc is not None and mc >= max_mc):
                mc_out += 1
                continue
        ticks = db.execute("SELECT ts, venue, tok_res, sol_res, real_tok, real_sol FROM ticks WHERE token=? "
                           "AND ts>=? AND ts<=? ORDER BY ts", (token, opened, opened + max_after_s)).fetchall()
        if not ticks:
            skipped += 1
            continue
        series = []
        for ts, venue, tr, sr, rt, rs in ticks:
            st = PoolState(venue, int(tr), int(sr), None if rt is None else int(rt), None if rs is None else int(rs))
            series.append((ts, st, fees.get(venue, 100), 1.0))
        # vault-priced venues (e.g. Meteora DAMM v2): the pool model is only a relative price;
        # scale each tick with the latest Jupiter calibration the bot stored for this token
        if series[0][1].venue not in ("pumpfun_curve", "pumpswap_amm"):
            try:
                cal = db.execute("SELECT ts, factor FROM pool_factors WHERE token=? ORDER BY ts", (token,)).fetchall()
            except sqlite3.OperationalError:
                cal = []
            if not cal:
                skipped += 1
                continue
            k, cur, scaled = 0, cal[0][1], []
            for ts, st, fee, _ in series:
                while k < len(cal) and cal[k][0] <= ts:
                    cur = cal[k][1]; k += 1
                scaled.append((ts, st, fee, cur))
            series = scaled
        if live:
            # our buy is really in the pool (no shadow); scale the pool model to real prices
            # with the first mark (real value, Jupiter-calibrated) vs the model at that moment
            if series[0][1].venue in ("pumpfun_curve", "pumpswap_amm"):
                factor = 1.0
                m = db.execute("SELECT ts, value FROM marks WHERE position_id=? ORDER BY ts LIMIT 1", (pid,)).fetchone()
                if m:
                    before = [x for x in series if x[0] <= m[0]] or series[:1]
                    _, st0, f0, _ = before[-1]
                    model = sell(st0, f0, int(amount), 0)[0] / L
                    if model > 0:
                        factor = m[1] / model
                series = [(ts, st, fee, factor) for ts, st, fee, _ in series]
            out.append(Pos(pid, cost, int(amount), 0, series))
            continue
        fee0 = series[0][2]
        entry = int((cost - paper_fee) * L)
        out.append(Pos(pid, cost, int(amount), entry * (10_000 - fee0) // 10_000, series))
    return out, skipped, mc_out


def run(p: Pos, style: str, a: float, b: float, sl: float, delay: float, tx_fee: float,
        stop_basis: str, part: float, confirm: float = 0.0, hard: float = 0.0) -> tuple[float, bool]:
    """Simulate one position. Returns (final SOL value after fees, censored).
    fixed: a=TP, b unused | trailing: a=activation, b=trail | partial: a=TP1, b=trail.
    confirm: protective exits (stop / trailing) only fire if the condition holds for
    `confirm` seconds; hard: a stop at -hard from the reference fires immediately anyway."""
    s = p.series
    held, net, cash = p.tokens, p.entry_net, 0.0
    ref = peak = None
    partial_done = False
    since = None                           # protective condition true since this time
    i = 0
    while i < len(s):
        ts, st, fee, f = s[i]
        trigger_ts = None
        if since is not None and ts >= since + confirm:
            trigger_ts, action = since + confirm, "all"     # held (state unchanged) until confirmed
        else:
            lam, _ = sell(st, fee, held, net)
            total = cash + lam / L * f
            if ref is None:
                ref = total
            peak = total if peak is None else max(peak, total)
            ret = total / p.cost - 1
            stop_ret = total / ref - 1 if stop_basis == "entry_price" else ret
            action, protective = None, False
            if style == "fixed":
                if ret >= a:
                    action = "all"
                elif stop_ret <= -sl:
                    action, protective = "all", True
            elif style == "trailing":
                if peak / p.cost - 1 >= a:
                    if total <= peak * (1 - b):
                        action, protective = "all", True
                elif stop_ret <= -sl:
                    action, protective = "all", True
            else:  # partial
                if not partial_done:
                    if ret >= a:
                        action = "part"
                    elif stop_ret <= -sl:
                        action, protective = "all", True
                elif total <= peak * (1 - b):
                    action, protective = "all", True
            if protective and confirm > 0 and not (hard > 0 and stop_ret <= -hard):
                if since is None:
                    since = ts
                if ts - since < confirm:
                    action = None
            elif not protective:
                since = None
            if action is not None:
                trigger_ts = ts
        if action is None:
            i += 1
            continue
        since = None
        j = i                              # fill on first tick >= trigger + delay
        while j < len(s) and s[j][0] < trigger_ts + delay:
            j += 1
        censored = j >= len(s)
        j = min(j, len(s) - 1)
        _, st2, fee2, f2 = s[j]
        if action == "all":
            lam2, _ = sell(st2, fee2, held, net)
            return cash + lam2 / L * f2 - tx_fee, censored
        n = int(held * part)
        lam2, gross = sell(st2, fee2, n, net)
        cash += lam2 / L * f2 - tx_fee
        held -= n
        net -= gross
        partial_done = True
        if censored:
            return cash, True
        i = j + 1
    ts, st, fee, f = s[-1]
    lam, _ = sell(st, fee, held, net)
    return cash + lam / L * f - tx_fee, True


def score(data, style, a, b, sl, delay, fee, basis, part, confirm=0.0, hard=0.0):
    pnl = wins = cens = 0
    for p in data:
        v, c = run(p, style, a, b, sl, delay, fee, basis, part, confirm, hard)
        pnl += v - p.cost; wins += v > p.cost; cens += c
    return pnl, wins, cens


def cell(r, n):
    return f"{r[0]:+.2f} {r[1]}/{n} c{r[2]}"


def table(title, rows_lbl, cols_lbl, fn, n):
    print(f"\n{title}")
    header = "".ljust(10) + "".join(f"{c:>17}" for c, _ in cols_lbl)
    print(header); print("-" * len(header))
    results = {}
    for rl, rv in rows_lbl:
        line = rl.ljust(10)
        for cl, cv in cols_lbl:
            r = fn(rv, cv)
            results[(rv, cv)] = (r, rl, cl)
            line += f"{cell(r, n):>17}"
        print(line)
    return results


def pct(x):
    return f"+{int(round(x * 100))}%"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="copybot.db")
    ap.add_argument("-c", "--config", default="config.yaml")
    ap.add_argument("--from-id", type=int, default=1)
    ap.add_argument("--delay", type=float, default=None, help="exit landing delay in s (default: config)")
    ap.add_argument("--sl", type=float, default=None, help="safety stop for trailing/partial (default: config)")
    ap.add_argument("--part", type=float, default=0.5, help="fraction sold at TP1 in the partial style")
    ap.add_argument("--max-after", type=float, default=7 * 86400, help="use ticks up to N s after entry")
    ap.add_argument("--live", action="store_true", help="analyse LIVE positions instead of paper ones")
    ap.add_argument("--tx-fee", type=float, default=None, help="SOL per sell transaction (default: paper fee, live 0.003)")
    ap.add_argument("--confirm", type=float, default=0.0,
                    help="protective exits must hold this many seconds before firing (main tables)")
    ap.add_argument("--hard", type=float, default=0.25, help="hard stop from the reference that never waits")
    ap.add_argument("--min-mc", type=float, default=None, help="only positions entered at >= this market cap (USD)")
    ap.add_argument("--max-mc", type=float, default=None, help="only positions entered below this market cap (USD)")
    args = ap.parse_args()
    try:
        cfg = yaml.safe_load(open(args.config))
    except Exception:
        cfg = {}
    sc = (cfg.get("chains") or {}).get("solana") or {}
    ex = cfg.get("exits") or {}
    fee = float(sc.get("paper_fee_per_tx_sol", 0))
    if args.tx_fee is not None:
        fee = args.tx_fee
    elif args.live:
        fee = 0.003
    fees = {"pumpfun_curve": int(sc.get("pumpfun_fee_bps", 125)), "pumpswap_amm": int(sc.get("pumpswap_fee_bps", 30)),
            "meteora_damm_v2": int(sc.get("meteora_damm_v2_fee_bps", 100))}
    delay = args.delay if args.delay is not None else float(sc.get("paper_land_delay_s", 1.5))
    sl = args.sl if args.sl is not None else float(ex.get("stop_loss", 0.10))
    basis = ex.get("stop_basis", "cost")

    data, skipped, mc_out = load(args.db, args.from_id, fees, float(sc.get("paper_fee_per_tx_sol", 0)),
                                 args.max_after, args.min_mc, args.max_mc, args.live)
    if args.live:
        print("LIVE positions (real fills; pool prices scaled with each position's first real mark)")
    n = len(data)
    if args.min_mc is not None or args.max_mc is not None:
        lo = f"${args.min_mc:,.0f}" if args.min_mc is not None else "$0"
        hi = f"${args.max_mc:,.0f}" if args.max_mc is not None else "any"
        print(f"market cap filter {lo} - {hi}: {mc_out} positions outside the range or without a logged market cap")
    print(f"{n} positions with tick data ({skipped} without: other venues or opened before ticks)")
    if not n:
        return
    print(f"exit delay {delay:g}s | tx fee {fee} SOL per sell | pool fees {fees} bps | stop basis {basis}"
          f" | stop confirmation {args.confirm:g}s (hard stop -{int(args.hard*100)}%)")
    print("Cells: total PnL in SOL, wins/trades, c = censored (data ended while still holding)")

    # --- how high did trades go? (the 'potential profit') ---
    print("\nHow far did positions run? (value if sold everything at the peak, within recorded data)")
    peaks, t_peaks = [], []
    for p in data:
        best, best_t = -1.0, 0.0
        for ts, st, f, k in p.series:
            r = sell(st, f, p.tokens, p.entry_net)[0] / L * k / p.cost - 1
            if r > best:
                best, best_t = r, ts - p.series[0][0]
        peaks.append(best); t_peaks.append(best_t)
    sp = sorted(peaks)
    print(f"  median peak {pct(sp[len(sp) // 2])}, best {pct(sp[-1])}, "
          f"median time to peak {sorted(t_peaks)[len(t_peaks) // 2] / 60:.1f} min")
    print("  reached: " + "   ".join(f"{pct(l)}: {sum(x >= l for x in peaks)}/{n}" for l in PEAK_LEVELS))
    print("  (peaks are hindsight: no exit rule catches them all)")

    allres = []   # (result, label, (style, a, b, stop))
    fx = table(f"FIXED take-profit / stop ({basis})   rows = stop, cols = take-profit",
               [(f"-{int(s_*100)}%", s_) for s_ in FIXED_SLS], [(pct(t), t) for t in FIXED_TPS],
               lambda s_, t: score(data, "fixed", t, 0, s_, delay, fee, basis, args.part, args.confirm, args.hard), n)
    for (s_, t), (r, rl, cl) in fx.items():
        allres.append((r, f"fixed TP {cl} / SL {rl}", ("fixed", t, 0, s_)))
    tr = table(f"TRAILING stop   rows = trail from peak, cols = activates at, safety stop -{int(sl*100)}%",
               [(f"{int(b*100)}% trail", b) for b in TRAIL_PCTS], [(pct(a), a) for a in TRAIL_ACTS],
               lambda b, a: score(data, "trailing", a, b, sl, delay, fee, basis, args.part, args.confirm, args.hard), n)
    for (b, a), (r, rl, cl) in tr.items():
        allres.append((r, f"trailing {rl} after {cl} (SL -{int(sl*100)}%)", ("trailing", a, b, sl)))
    pa = table(f"PARTIAL: sell {int(args.part*100)}% at TP1, rest trails   rows = trail, cols = TP1, "
               f"safety stop -{int(sl*100)}%",
               [(f"{int(b*100)}% trail", b) for b in PART_TRAIL], [(pct(a), a) for a in PART_TP1],
               lambda b, a: score(data, "partial", a, b, sl, delay, fee, basis, args.part, args.confirm, args.hard), n)
    for (b, a), (r, rl, cl) in pa.items():
        allres.append((r, f"partial {int(args.part*100)}% at {cl}, rest {rl} (SL -{int(sl*100)}%)",
                       ("partial", a, b, sl)))

    max_c = math.ceil(n * 0.2)
    ok = [x for x in allres if x[0][2] <= max_c]
    print(f"\nTOP 8 across all styles (only settings with <= {max_c} censored trades; "
          f"{len(allres) - len(ok)} excluded as unreliable)")
    for r, label, _ in sorted(ok, key=lambda x: -x[0][0])[:8]:
        print(f"  {r[0]:+7.2f} SOL  {r[1]:>3}/{n} wins  c{r[2]:<3} {label}")
    if ok:
        r, label, (style, a, b, s_) = max(ok, key=lambda x: x[0][0])
        print(f"\nWhat speed is worth for the best one ({label}):")
        for d in (0.0, 0.4, 1.0, 1.5, 3.0, 6.0):
            pnl, wins, cens = score(data, style, a, b, s_, d, fee, basis, args.part, args.confirm, args.hard)
            print(f"  {d:>4.1f}s  {pnl:+.2f} SOL  ({wins}/{n} wins, {cens} censored)")
    print(f"\nStop confirmation (protective exits must hold N seconds; hard stop -{int(args.hard*100)}% never waits):")
    refs = [("fixed TP +30% / SL -10%", ("fixed", 0.30, 0, 0.10)),
            ("fixed TP +100% / SL -10%", ("fixed", 1.00, 0, 0.10)),
            (f"trailing 30% after +50% / SL -{int(sl*100)}%", ("trailing", 0.50, 0.30, sl))]
    confirms = (0, 1, 2, 3, 5)
    print("  " + "".ljust(34) + "".join(f"{f'{c}s':>17}" for c in confirms))
    for label, (style, a, b, s_) in refs:
        row = "  " + label.ljust(34)
        for c in confirms:
            row += f"{cell(score(data, style, a, b, s_, delay, fee, basis, args.part, c, args.hard), n):>17}"
        print(row)

    print(f"\n{len(allres)} settings tested on {n} trades: the best one is partly luck (overfitting).")
    print("Before changing the bot: check the same setting is also good on LATER trades (--from-id).")
    if n < 30:
        print(f"Only {n} trades: read this as direction, not as settings.")


if __name__ == "__main__":
    main()
