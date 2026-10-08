"""What-if on TICK data: replay every pool update and compare exit styles.

Usage (from the copybot folder):
    python whatif_ticks.py --from-id 44              # newer positions only
    python whatif_ticks.py --from-id 44 --delay 0.5  # exits land 0.5 s after the trigger
    python whatif_ticks.py --from-id 44 --sl 0.15    # safety stop used by trailing/partial styles
    python whatif_ticks.py --from-id 44 --max-mc 500000              # only entries below $500k market cap
    python whatif_ticks.py --from-id 44 --min-mc 500000              # only entries at/above $500k
    python whatif_ticksv2.py --live --leader-sl 0.4                  # leader-exit style, -40% safety stop
    python whatif_ticksv2.py --from-id 230 --no-exclude-profiles     # include positions of leader profiles

Positions opened under a leader profile (bot/profiles.py, timing["profile"]) are EXCLUDED by default: they
ran other exits and sizes, so they would pollute a hypothesis on the global exits. The excluded ones are
listed (id, leader, profile). --profile NAME looks at one profile only ('default' = positions without one).

DATA RULE (scripts/flags.py): positions with an unconfirmed single-step mark change > 50% are excluded by
default and listed; --include-flagged keeps them. A tick series must COVER our entry (first tick within
--cover-s of the fill), otherwise the position is "not replayable" (a series that starts after our close
would replay someone else's trade). All ticks after our close are used (up to --max-after).
Old positions whose raw ticks were compacted (scripts/compact_ticks.py) replay on 1-minute OHLC bars
(ticks_1m: 4 points per minute - open, low/high, close): exits land at minute precision, the peak is
the bar high, so results are coarser than on raw ticks; their count is printed.

Exit styles compared
  fixed     sell everything at +TP, or at the stop                (what the bot does now)
  trailing  once up +A%, sell everything when value falls X% from its peak; stop before that
  partial   sell part at +TP1, the rest with a trailing stop X% from the peak; stop before TP1
  leader    sell everything when the copied leader sells (his first sell of that token after our
            entry, at the time the bot saw it); optional wide safety stop (--leader-sl), optionally
            combined with the trailing stop. No leader sell in the data = censored.

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
import json
import math
import os
import sqlite3
from dataclasses import dataclass

import yaml

from bot.pricing.pools import PoolState
from scripts.flags import flagged_note, flagged_positions

L = 1_000_000_000
FIXED_TPS = [0.10, 0.20, 0.30, 0.50, 1.00, 2.00, 4.00]          # +100% = 2x, +200% = 3x, +400% = 5x
FIXED_SLS = [0.05, 0.10, 0.15, 0.20, 0.30]
TRAIL_ACTS = [0.10, 0.20, 0.30, 0.50, 1.00]
TRAIL_PCTS = [0.10, 0.20, 0.30, 0.40]
PART_TP1 = [0.20, 0.30, 0.50, 1.00]
PART_TRAIL = [0.15, 0.25, 0.40]
PEAK_LEVELS = [0.30, 0.50, 1.00, 2.00, 4.00]
LEADER_SLS = [0.0, 0.20, 0.30, 0.40, 0.60]                     # 0 = no safety stop


@dataclass
class Pos:
    pid: int
    cost: float          # SOL incl. paper buy fee
    tokens: int          # raw
    entry_net: int       # lamports that entered the pool from our buy (after pool fee)
    series: list         # [(ts, PoolState, fee_bps, factor)]  factor = real value / pool-model value
    factor: float = 1.0  # kept for compatibility
    leader_exit: float | None = None   # when the bot saw the leader's first sell after our entry
    resolution: str = "tick"           # "tick" = raw pool updates, "1m" = compacted 1-minute bars


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


def leader_exit_ts(db, token, leader, opened):
    """First 'sell' signal of this leader for this token after our entry (bot detection time).
    The bot logs every leader sell, also after our position closed."""
    r = db.execute("SELECT MIN(ts) FROM signals WHERE token=? AND leader=? AND side='sell' AND ts>?",
                   (token, leader, opened)).fetchone()
    return r[0] if r else None


def profile_of(timing) -> str:
    """Leader profile a position was opened under (bot/profiles.py), 'default' if none."""
    return json.loads(timing or "{}").get("profile") or "default"


def keep_profile(timing, profile: str = "", exclude_profiles: bool = False) -> bool:
    """--profile NAME keeps only that profile ('default' = none); otherwise --exclude-profiles drops every
    position opened under a leader profile, so a hypothesis on the global exits is judged on its own trades."""
    prof = profile_of(timing)
    if profile:
        return prof == profile
    return not (exclude_profiles and prof != "default")


def bars_to_ticks(rows):
    """ticks_1m rows -> pseudo ticks: open at ts, low and high (further-from-open one last) at +20/+40 s, close at
    +59 s. Only the close carries real_* reserves; open/high/low use the close's (curve caps are approximate)."""
    out = []
    for ts, venue, n, to, so, th, sh, tl, sl, tc, sc, rtc, rsc in rows:
        rt = None if rtc is None else int(rtc)
        rs = None if rsc is None else int(rsc)
        p_open, p_hi, p_lo = int(so) / int(to), int(sh) / int(th), int(sl) / int(tl)
        mid = [(ts + 20, int(tl), int(sl)), (ts + 40, int(th), int(sh))]
        if abs(p_hi - p_open) < abs(p_lo - p_open):          # the move closer to the open probably came first
            mid = [(ts + 20, int(th), int(sh)), (ts + 40, int(tl), int(sl))]
        for t, tok, sol in [(ts, int(to), int(so))] + mid + [(ts + 59, int(tc), int(sc))]:
            out.append((t, venue, tok, sol, rt, rs))
    return out


def load(db_path, from_id, fees, paper_fee, max_after_s, min_mc=None, max_mc=None, live=False, profile="",
         exclude_profiles=False, cover_s=30.0, exclude_flagged=True):
    """-> (positions, skipped, mc_out, excluded, dropped): excluded = [(id, leader, profile)] dropped by the
    profile filter; dropped = {"not_covered": [(id, seconds between fill and first tick)],
    "flagged": [(id, leader, reason)], "bars_1m": [ids replayed on 1-minute bars]}."""
    # read-only, so it is safe to point --db at the live bot's database while it runs
    db = sqlite3.connect(db_path if db_path.startswith("file:") else
                         f"file:{os.path.abspath(os.path.expanduser(db_path))}?mode=ro", uri=True)
    flagged = flagged_positions(db) if exclude_flagged else {}
    rows = db.execute("SELECT id, token, token_amount, cost, opened_at, leader, timing FROM positions WHERE paper=? "
                      "AND chain='solana' AND status='closed' AND id>=? ORDER BY id", (0 if live else 1, from_id)).fetchall()
    excluded = [(r[0], r[5], profile_of(r[6])) for r in rows if not keep_profile(r[6], profile, exclude_profiles)]
    rows = [r for r in rows if keep_profile(r[6], profile, exclude_profiles)]
    dropped = {"not_covered": [], "flagged": [(r[0], r[5], flagged[r[0]]) for r in rows if r[0] in flagged],
               "bars_1m": []}
    rows = [r for r in rows if r[0] not in flagged]
    out, skipped, mc_out = [], 0, 0
    has_bars = bool(db.execute("SELECT 1 FROM sqlite_master WHERE name='ticks_1m'").fetchone())
    for pid, token, amount, cost, opened, leader, timing in rows:
        if min_mc is not None or max_mc is not None:
            mc = entry_mc(db, token, leader, opened)
            if mc is None or (min_mc is not None and mc < min_mc) or (max_mc is not None and mc >= max_mc):
                mc_out += 1
                continue
        filled = json.loads(timing or "{}").get("filled_at") or opened
        ticks = db.execute("SELECT ts, venue, tok_res, sol_res, real_tok, real_sol FROM ticks WHERE token=? "
                           "AND ts>=? AND ts<=? ORDER BY ts", (token, opened, opened + max_after_s)).fetchall()
        resolution, slack = "tick", 0.0
        if has_bars and (not ticks or ticks[0][0] > filled + cover_s):
            # compacted: 1-minute bars up to the first raw tick (or the whole window), raw ticks after that
            until = ticks[0][0] if ticks else opened + max_after_s
            bars = db.execute("SELECT ts, venue, n, tok_open, sol_open, tok_high, sol_high, tok_low, sol_low, tok_close, "
                              "sol_close, real_tok_close, real_sol_close FROM ticks_1m WHERE token=? AND ts>=? AND ts<? "
                              "ORDER BY ts", (token, opened - 60, until)).fetchall()
            if bars:
                ticks = [t for t in bars_to_ticks(bars) if opened - 60 <= t[0] < until] + ticks
                resolution, slack = "1m", 60.0
        if not ticks:
            skipped += 1
            continue
        if ticks[0][0] > filled + cover_s + slack:            # the series does not cover our entry
            dropped["not_covered"].append((pid, round(ticks[0][0] - filled)))
            continue
        if resolution == "1m":
            dropped["bars_1m"].append(pid)
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
            out.append(Pos(pid, cost, int(amount), 0, series,
                           leader_exit=leader_exit_ts(db, token, leader, opened), resolution=resolution))
            continue
        fee0 = series[0][2]
        entry = int((cost - paper_fee) * L)
        out.append(Pos(pid, cost, int(amount), entry * (10_000 - fee0) // 10_000, series,
                       leader_exit=leader_exit_ts(db, token, leader, opened), resolution=resolution))
    return out, skipped, mc_out, excluded, dropped


def run(p: Pos, style: str, a: float, b: float, sl: float, delay: float, tx_fee: float,
        stop_basis: str, part: float, confirm: float = 0.0, hard: float = 0.0) -> tuple[float, bool]:
    """Simulate one position. Returns (final SOL value after fees, censored).
    fixed: a=TP, b unused | trailing: a=activation, b=trail | partial: a=TP1, b=trail.
    leader: sell all at p.leader_exit; sl 0 = no safety stop; b > 0 adds a trailing stop
    (a=activation, b=trail) that can exit before the leader does.
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
            elif style == "leader":
                if p.leader_exit is not None and ts >= p.leader_exit:
                    action = "all"
                elif b > 0 and peak / p.cost - 1 >= a:
                    if total <= peak * (1 - b):
                        action, protective = "all", True
                elif sl > 0 and stop_ret <= -sl:
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
                if style == "leader" and not protective and p.leader_exit is not None:
                    trigger_ts = max(p.leader_exit, s[0][0])   # his sell, not the next tick after it
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


VENUE_CLASSES = (("pool", "pump.fun curve / PumpSwap: exact pool math, streamed"),
                 ("vault", "Meteora DAMM v2: vault balances x Jupiter factor, streamed"),
                 ("jupiter", "other venues: Jupiter quotes, polled - replayable only if a pool was recorded later"))


def venue_class(timing: dict, pos: "Pos | None") -> str:
    """How the bot priced this position: from the fill model it logged ('jupiter+0.8s', 'pumpswap_amm+0.8s'...),
    else from the replayed ticks, else 'jupiter' (no ticks = not pool-priced)."""
    model = (timing.get("fill_model") or "").split("+")[0]
    venue = model or (pos.series[0][1].venue if pos else "jupiter")
    if venue in ("pumpfun_curve", "pumpswap_amm"):
        return "pool"
    return "jupiter" if venue in ("jupiter", "live", "") else "vault"


def venue_groups(db_path, from_id, live, data, min_mc, max_mc, profile="", exclude_profiles=False,
                 exclude_flagged=True):
    """Closed positions per pricing class: {class: [(id, cost, exit_value, exit_reason)]}, and the ones the
    profile filter dropped: [(id, leader, profile)]. Same filters as load() (flagged positions dropped too)."""
    db = sqlite3.connect(db_path if db_path.startswith("file:") else
                         f"file:{os.path.abspath(os.path.expanduser(db_path))}?mode=ro", uri=True)
    flagged = flagged_positions(db) if exclude_flagged else {}
    by_id = {p.pid: p for p in data}
    groups, excluded = {c: [] for c, _ in VENUE_CLASSES}, []
    for pid, token, leader, cost, exitv, opened, reason, timing in db.execute(
            "SELECT id, token, leader, cost, exit_value, opened_at, exit_reason, timing FROM positions WHERE paper=? "
            "AND chain='solana' AND status='closed' AND id>=? ORDER BY id", (0 if live else 1, from_id)):
        if not keep_profile(timing, profile, exclude_profiles):
            excluded.append((pid, leader, profile_of(timing)))
            continue
        if pid in flagged:
            continue
        if min_mc is not None or max_mc is not None:
            mc = entry_mc(db, token, leader, opened)
            if mc is None or (min_mc is not None and mc < min_mc) or (max_mc is not None and mc >= max_mc):
                continue
        t = json.loads(timing or "{}")
        groups[venue_class(t, by_id.get(pid))].append((pid, cost, exitv, reason))
    return groups, excluded


def venue_split(db_path, from_id, live, data, min_mc, max_mc, styles, delay, fee, basis, part, confirm, hard,
                profile="", exclude_profiles=False, exclude_flagged=True):
    """Report-only: actual results and the what-if per pricing class (pool / vault / Jupiter)."""
    by_id = {p.pid: p for p in data}
    groups, _ = venue_groups(db_path, from_id, live, data, min_mc, max_mc, profile, exclude_profiles, exclude_flagged)
    print("\nVENUE SPLIT (report only): ACTUAL closed results per pricing class, and the what-if where replayable")
    hdr = f"  {'class':<8} {'n':>3} {'wins':>5} {'PnL SOL':>8} {'avg %':>6} {'median %':>8} {'stop fill med':>13}"
    print(hdr + "".join(f"{lbl[:22]:>24}" for lbl, _ in styles))
    for cls, desc in VENUE_CLASSES:
        g = groups[cls]
        if not g:
            print(f"  {cls:<8} {0:>3}   ({desc})")
            continue
        r = sorted((e - c) / c for _, c, e, _ in g)
        stops = sorted((e - c) / c for _, c, e, why in g if why == "stop_loss")
        row = (f"  {cls:<8} {len(g):>3} {sum(x > 0 for x in r):>5} {sum(e - c for _, c, e, _ in g):>+8.2f} "
               f"{100 * sum(r) / len(r):>+6.1f} {100 * r[len(r) // 2]:>+8.1f} "
               f"{(f'{100 * stops[len(stops) // 2]:+.1f}% (n={len(stops)})') if stops else '-':>13}")
        sub = [by_id[pid] for pid, *_ in g if pid in by_id]
        for _, (style, a, b, s_) in styles:
            row += f"{(cell(score(sub, style, a, b, s_, delay, fee, basis, part, confirm, hard), len(sub)) if sub else 'no ticks'):>24}"
        print(row + f"   ({desc})")
    print("  what-if cells: model PnL on the replayable positions of that class (pool model is a bit optimistic "
          "vs actual fills)")


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
    ap.add_argument("--profile", default="", help="only positions opened under this leader profile ('default' = none)")
    ap.add_argument("--exclude-profiles", action=argparse.BooleanOptionalAction, default=True,
                    help="drop positions opened under any leader profile (default on; ignored with --profile)")
    ap.add_argument("--include-flagged", action="store_true",
                    help="keep positions the DATA RULE flags (unconfirmed single-step mark change > 50%%)")
    ap.add_argument("--cover-s", type=float, default=30.0,
                    help="a tick series must start within this many seconds of our fill to be replayable")
    ap.add_argument("--leader-sl", type=float, default=None,
                    help="extra safety-stop row for the leader-exit style, e.g. 0.4 (rows always include off/20/30/40/60%%)")
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

    excl = args.exclude_profiles and not args.profile
    data, skipped, mc_out, excluded, dropped = load(args.db, args.from_id, fees, float(sc.get("paper_fee_per_tx_sol", 0)),
                                                    args.max_after, args.min_mc, args.max_mc, args.live, args.profile,
                                                    excl, args.cover_s, not args.include_flagged)
    print(flagged_note({pid: why for pid, _, why in dropped["flagged"]}, args.include_flagged))
    for pid, leader, why in dropped["flagged"]:
        print(f"  #{pid:<5} {leader[:8]}  {why}")
    if dropped["not_covered"]:
        print(f"not replayable (ticks start > {args.cover_s:g}s after our fill): "
              + ", ".join(f"#{pid} (+{dt}s)" for pid, dt in dropped["not_covered"]))
    if dropped["bars_1m"]:
        print(f"{len(dropped['bars_1m'])} position(s) replayed on 1-minute bars (compacted ticks, minute precision): "
              + ", ".join(f"#{pid}" for pid in dropped["bars_1m"]))
    if args.profile:
        print(f"profile filter: {args.profile}")
    elif excl:
        print(f"leader-profile positions excluded: {len(excluded)}"
              + (" (--no-exclude-profiles to include them)" if excluded else ""))
        for pid, leader, prof in excluded:
            print(f"  #{pid:<5} {leader[:8]}  {prof}")
    else:
        print("leader-profile positions INCLUDED (--exclude-profiles to drop them)")
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

    t_act, t_pct = float(ex.get("trailing_activate", 0.5) or 0.5), float(ex.get("trailing_pct", 0.3) or 0.3)
    has = [p for p in data if p.leader_exit is not None and p.leader_exit <= p.series[-1][0]]
    waits = sorted(p.leader_exit - p.series[0][0] for p in has)
    print(f"\nLeader exits: {len(has)}/{n} positions have the leader's first sell inside the recorded data"
          + (f" (median {waits[len(waits) // 2] / 60:.1f} min after our entry)" if waits else "")
          + f"; the other {n - len(has)} are censored unless a stop fires")
    lsls = sorted(set(LEADER_SLS) | ({args.leader_sl} if args.leader_sl is not None else set()))
    le = table(f"LEADER EXIT: sell when the leader sells   rows = safety stop ({basis}), "
               f"cols = leader exit only / + trailing {int(t_pct*100)}% after {pct(t_act)}",
               [("no stop" if x == 0 else f"-{int(round(x*100))}%", x) for x in lsls],
               [("leader only", (0.0, 0.0)), (f"+trail {pct(t_act)}", (t_act, t_pct))],
               lambda s_, ab: score(data, "leader", ab[0], ab[1], s_, delay, fee, basis, args.part,
                                    args.confirm, args.hard), n)
    for (s_, ab), (r, rl, cl) in le.items():
        allres.append((r, f"leader exit{'' if ab[1] == 0 else f' + trailing {int(ab[1]*100)}% after {pct(ab[0])}'}"
                          f" ({'no stop' if s_ == 0 else f'SL -{int(round(s_*100))}%'})", ("leader", ab[0], ab[1], s_)))

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

    cur = (("fixed", float(ex.get("take_profit")), 0, float(ex.get("stop_loss", 0.1)))
           if float(ex.get("take_profit") or 0) > 0 else ("trailing", t_act, t_pct, sl))
    venue_split(args.db, args.from_id, args.live, data, args.min_mc, args.max_mc,
                [("config exits", cur), ("fixed TP +30% / SL -20%", ("fixed", 0.30, 0, 0.20)),
                 ("trail 30%/+50% SL -20%", ("trailing", 0.50, 0.30, 0.20))],
                delay, fee, basis, args.part, args.confirm, args.hard, args.profile, excl, not args.include_flagged)

    print(f"\n{len(allres)} settings tested on {n} trades: the best one is partly luck (overfitting).")
    print("Before changing the bot: check the same setting is also good on LATER trades (--from-id).")
    if n < 30:
        print(f"Only {n} trades: read this as direction, not as settings.")


if __name__ == "__main__":
    main()
