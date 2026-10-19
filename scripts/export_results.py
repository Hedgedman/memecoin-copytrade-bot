"""Public, anonymized results export (read-only on both databases):

    python -m scripts.export_results [--paper copybot.db] [--live ~/Documents/copybot-live/copybot.db]
                                     [--leaders-map leaders_map.local.txt] [--out results] [-c config.yaml]

Writes results/: positions_paper.csv, positions_live.csv (one row per CLOSED position), tables/*.md, charts/*.png
and RESULTS.md (tables + charts, facts only). Nothing in the output identifies a wallet or a token: leaders are
pseudonyms from the local map (Leader-A.., never committed), positions carry no token / signature column, dates are
rounded to the day. Before it finishes the script scans every text output for address patterns and for the 8-character
prefixes of every leader / token string in both databases, and fails (deleting nothing, writing nothing public) if it
finds one.

Definitions (also printed in RESULTS.md):
  period   before / after position #366 (the dev restart of 2026-10-08 with the H6 / profile build). Live positions have
           their own ids, so they are placed by opening time against the opening time of paper position #366.
  venue    how the fill was priced: curve (pump.fun curve), pumpswap, jupiter, vault (Meteora), evm, unknown (no
           venue logged and no pool tick near the fill).
  gap      our fill price (cost / tokens, all-in) vs the leader's price in his swap, % (+ = we paid more).
           EVM: median gap_pct of the position's buy fills.
  flagged  excluded from every table by the data rules: unconfirmed single-step mark change > 50% (scripts/flags.py),
           flagged by the bot itself, or open during the dev bot downtime (DOWNTIME).
Tables use Solana, non-flagged, non-profile paper positions unless the heading says otherwise."""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sqlite3
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timezone

import yaml

import whatif_ticksv2 as W
from scripts import live_vs_paper as LP
from scripts.flags import flagged_positions
from scripts.leader_report import solana_gap

REF_ID = 366                         # period boundary (paper id)
H5_FROM = 230                        # H4 / H5 start (paper id)
H8_FIRST_LIVE_ID = 55                # H8-LIVE = live ids 55-61 (pilot 55-56, v2 57-61)
H11_FROM = 520                       # H11 forward test / corrected PumpSwap prices start here
DOWNTIME = (1791664526.9, 1791722485.3)       # dev bot down 2026-10-10 20:35 -> 2026-10-11 12:41 UTC
S1_EXCLUDE = ("Leader-D", "Leader-F", "Leader-O", "Leader-M")      # H11 S1: all Solana leaders except these
S2_LEADERS = ("Leader-G", "Leader-E", "Leader-H")                  # H11 S2
MC_EDGES = ((30_000, "<30k"), (100_000, "30-100k"), (250_000, "100-250k"), (500_000, "250-500k"))
COLUMNS = ["id", "chain", "opened_date", "period", "leader", "venue", "entry_mc_bucket", "entry_gap_pct", "pnl_pct",
           "exit_reason", "hold_s", "flagged", "flag_reason", "profile", "cost", "pnl", "unit"]
WINDOW_S = 24 * 3600
LEVELS = (1.3, 1.5, 2.0, 3.0, 5.0, 10.0)
ADDR_RE = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b|\b0x[0-9a-fA-F]{40}\b")

INK, INK2, GRID, SURFACE = "#0b0b0b", "#52514e", "#e4e3df", "#fcfcfb"
C1, C2, C3 = "#2a78d6", "#eb6834", "#1baf7a"                       # categorical slots 1-3


# ----------------------------------------------------------------------------- helpers
def open_ro(path: str) -> sqlite3.Connection:
    db = sqlite3.connect(f"file:{os.path.abspath(os.path.expanduser(path))}?mode=ro", uri=True, timeout=30)
    db.execute("PRAGMA busy_timeout=30000")
    return db


def load_leader_map(path: str) -> dict[str, str]:
    """leaders_map.local.txt (tab separated: pseudonym, solana address, evm address, index) -> {address: pseudonym}."""
    m: dict[str, str] = {}
    for line in open(os.path.expanduser(path)):
        if line.startswith("#") or not line.strip():
            continue
        p = line.rstrip("\n").split("\t")
        m[p[1]] = p[0]
        if len(p) > 2 and p[2]:
            m[p[2].lower()] = p[0]
    return m


def pseudonym(leader: str, lmap: dict[str, str]) -> str:
    name = lmap.get(leader) or lmap.get(leader.lower())
    if not name:
        raise KeyError("a leader in the database is not in the leaders map (refusing to export it)")
    return name


def day(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")


def mc_bucket(mc) -> str:
    if mc is None:
        return "n/a"
    for edge, label in MC_EDGES:
        if mc < edge:
            return label
    return ">=500k"


def med(xs):
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else None


def f(x, spec="+.1f", unit=""):
    return "-" if x is None else f"{x:{spec}}{unit}"


def md_table(headers, rows, align=None) -> str:
    align = align or ["l"] + ["r"] * (len(headers) - 1)
    sep = ["---:" if a == "r" else ":---" for a in align]
    out = ["| " + " | ".join(headers) + " |", "| " + " | ".join(sep) + " |"]
    out += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    return "\n".join(out)


def pnl_stats(rows):
    """n, wins, PnL, PnL without best 1 / best 3, mean and median return %."""
    p = sorted((r["pnl"] for r in rows), reverse=True)
    pct = [r["pnl_pct"] for r in rows if r["pnl_pct"] is not None]
    return {"n": len(p), "wins": sum(x > 0 for x in p), "pnl": sum(p),
            "wo1": sum(p[1:]) if len(p) > 1 else None, "wo3": sum(p[3:]) if len(p) > 3 else None,
            "avg": sum(pct) / len(pct) if pct else None, "med": med(pct)}


# ----------------------------------------------------------------------------- positions
def venue_of(db, chain: str, token: str, opened: float, t: dict) -> str:
    if chain != "solana":
        return "evm"
    model = (t.get("fill_model") or "").split("+")[0]
    if model == "live":
        model = (t.get("quote_source") or "").split("*")[0]
    known = {"pumpfun_curve": "curve", "pumpswap_amm": "pumpswap", "jupiter": "jupiter"}
    if model in known:
        return known[model]
    if model.startswith("meteora"):
        return "vault"
    r = db.execute("SELECT venue FROM ticks WHERE token=? AND ts BETWEEN ? AND ? ORDER BY ts LIMIT 1",
                   (token, opened - 60, opened + 60)).fetchone()
    return known.get(r[0], "vault" if r and r[0].startswith("meteora") else "unknown") if r else "unknown"


def downtime_ids(db) -> set[int]:
    return {r[0] for r in db.execute("SELECT id FROM positions WHERE opened_at<? AND (closed_at IS NULL OR closed_at>?)",
                                     (DOWNTIME[1], DOWNTIME[0]))}


def collect_positions(db, lmap, ref_id=REF_ID, ref_ts=None, paper=True, downtime=True) -> list[dict]:
    """One row per closed position. `ref_ts` (opening time of paper #366) places live positions in a period."""
    flagged = flagged_positions(db)
    down = downtime_ids(db) if downtime else set()
    out = []
    for p in db.execute("SELECT id, chain, token, leader, token_amount, cost, opened_at, status, exit_value, closed_at, "
                        "exit_reason, timing FROM positions WHERE paper=? AND status='closed' AND exit_value IS NOT NULL "
                        "ORDER BY id", (1 if paper else 0,)):
        pid, chain, token, leader, amount, cost, opened, _, exitv, closed, reason, timing = p
        t = json.loads(timing or "{}")
        cost, exitv = float(cost), float(exitv)
        if chain == "solana":
            mc = t.get("fill_mc_usd")
            if mc is None:
                mc = W.entry_mc(db, token, leader, opened)
            gap = solana_gap(db, {"leader": leader, "token": token, "opened_at": opened, "cost": cost,
                                  "token_amount": amount})
        else:
            mc = None
            gap = med(r[0] for r in db.execute("SELECT gap_pct FROM xchain_fills WHERE position_id=? AND side='buy'",
                                               (pid,)))
        reasons = []
        if pid in flagged:
            reasons.append("bot_flag" if flagged[pid].startswith("bot:") else "mark_step")
        if pid in down:
            reasons.append("downtime")
        before = pid < ref_id if paper else opened < (ref_ts if ref_ts is not None else 0)
        out.append({"id": pid, "chain": chain, "opened_date": day(opened), "period": "before" if before else "after",
                    "leader": pseudonym(leader, lmap), "venue": venue_of(db, chain, token, opened, t),
                    "entry_mc_bucket": mc_bucket(mc), "entry_gap_pct": None if gap is None else round(gap, 1),
                    "pnl_pct": round(100 * (exitv / cost - 1), 1) if cost else None, "exit_reason": reason or "",
                    "hold_s": round(closed - opened), "flagged": "yes" if reasons else "no",
                    "flag_reason": "+".join(reasons), "profile": "yes" if t.get("profile") else "no",
                    "cost": round(cost, 4), "pnl": round(exitv - cost, 4), "unit": "SOL" if chain == "solana" else "USD"})
    return out


def write_csv(path: str, rows: list[dict]) -> None:
    with open(path, "w") as fh:
        fh.write(",".join(COLUMNS) + "\n")
        for r in rows:
            fh.write(",".join("" if r[c] is None else str(r[c]) for c in COLUMNS) + "\n")


def analysis(rows):
    """Solana, non-flagged, non-profile (the rows every table uses)."""
    return [r for r in rows if r["chain"] == "solana" and r["flagged"] == "no" and r["profile"] == "no"]


# ----------------------------------------------------------------------------- tables
def table_h5(paper_rows) -> str:
    groups = (("pool (curve + pumpswap)", ("curve", "pumpswap")), ("  curve", ("curve",)), ("  pumpswap", ("pumpswap",)),
              ("vault (Meteora)", ("vault",)), ("jupiter", ("jupiter",)), ("unknown", ("unknown",)))
    ranges = ((f"#{H5_FROM}+", H5_FROM, 10**9), (f"#{H5_FROM}-{REF_ID - 1}", H5_FROM, REF_ID - 1),
              (f"#{REF_ID}+", REF_ID, 10**9))
    body = []
    for label, lo, hi in ranges:
        base = [r for r in analysis(paper_rows) if lo <= r["id"] <= hi]
        for name, venues in groups:
            rs = [r for r in base if r["venue"] in venues]
            if not rs:
                continue
            s = pnl_stats(rs)
            stops = [r["pnl_pct"] for r in rs if r["exit_reason"] == "stop_loss"]
            body.append((label, name.strip() if name.startswith("  ") else name, s["n"], f"{s['wins']}/{s['n']}",
                         f(s["pnl"], "+.2f"), f(s["wo3"], "+.2f"), f(s["avg"], "+.1f", "%"), f(s["med"], "+.1f", "%"),
                         f"{f(med(stops), '+.1f', '%')} (n={len(stops)})" if stops else "-"))
    return md_table(["ids", "class", "n", "wins", "PnL SOL", "w/o best 3", "avg %", "median %", "stop fill median"], body,
                    ["l", "l", "r", "r", "r", "r", "r", "r", "r"])


def table_h8(live_rows, paper_rows, cmp_rows, s) -> str:
    by_live = {r["id"]: r for r in live_rows}
    by_paper = {r["id"]: r for r in paper_rows}
    closed = [r for r in cmp_rows if r["pnl"] is not None]
    stops = sum(r["live_exit"] == "stop_loss" for r in closed)
    costs_live = [by_live[r["id"]]["cost"] for r in cmp_rows if r["id"] in by_live]
    costs_paper = [by_paper[r["paper_id"]]["cost"] for r in cmp_rows if r.get("paper_id") in by_paper]
    summary = [("live positions (closed)", f"{len(closed)}"), ("matched with a paper position", f"{s['matched']}"),
               ("exits by stop_loss (live)", f"{stops}/{len(closed)}"),
               ("realized PnL live (SOL)", f(s["total"], "+.3f")),
               ("buys that failed on the slippage limit", f"{s['missed']} (vs {s['filled']} filled)"),
               ("realized buy slippage vs Jupiter quote, median", f"{f(s['median_buy_slip'], '+.1f', '%')} (n={s['n_slip']})"),
               ("fill price live vs paper, median / mean", f"{f(s['median_fill_gap'], '+.1f', '%')} / {f(s['mean_fill_gap'], '+.1f', '%')}"),
               ("entry gap vs leader, live, median", f"{f(s['median_leader_gap'], '+.1f', '%')} (n={s['n_leader_gap']})"),
               ("stop fill live minus paper, common stops", f"{f(s['stop_points'], '+.1f', ' points')} (n={s['n_stops']})"),
               ("position size, median (SOL): live / paper", f"{f(med(costs_live), '.2f')} / {f(med(costs_paper), '.2f')}")]
    per = []
    for r in cmp_rows:
        lr = by_live.get(r["id"], {})
        per.append((r["id"], day(r["opened_at"]), pseudonym_safe(r), lr.get("venue", "-"), f(r["fill_gap"], "+.1f", "%"),
                    f(r["buy_slip"], "+.1f", "%"), f(r["live_leader_gap"], "+.1f", "%"), f(r["paper_leader_gap"], "+.1f", "%"),
                    f"{r['live_exit'] or '-'} {f(r['live_ret'], '+.1f', '%')}",
                    f"{r['paper_exit'] or '-'} {f(r['paper_ret'], '+.1f', '%')}" if r["paper_id"] else "no match",
                    f(r["points"], "+.1f")))
    return (md_table(["H8-LIVE (live ids %d+)" % H8_FIRST_LIVE_ID, "value"], summary, ["l", "r"]) + "\n\n"
            + md_table(["live id", "date", "leader", "venue", "fill live vs paper", "buy slip", "gap live", "gap paper",
                        "live exit", "paper exit", "points"], per, ["r", "l", "l", "l"] + ["r"] * 7))


def pseudonym_safe(r) -> str:
    return r.get("_leader", "-")


def leader_results(paper_rows):
    """{leader: {period: [rows]}} on the analysis set."""
    out = defaultdict(lambda: {"before": [], "after": []})
    for r in analysis(paper_rows):
        out[r["leader"]][r["period"]].append(r)
    return out


def leader_key(name: str):
    s = name.split("-", 1)[-1]
    return (len(s), s)


def table_leaders(paper_rows) -> str:
    res = leader_results(paper_rows)
    body = []
    for name in sorted(res, key=leader_key):
        cells = [name]
        for per in ("before", "after"):
            s = pnl_stats(res[name][per])
            cells += [s["n"], f(s["pnl"], "+.2f") if s["n"] else "-", f(s["wo1"], "+.2f"),
                      f(s["avg"], "+.1f", "%")] if s["n"] else [0, "-", "-", "-"]
        body.append(cells)
    tot = []
    for per in ("before", "after"):
        rs = [r for r in analysis(paper_rows) if r["period"] == per]
        s = pnl_stats(rs)
        tot += [s["n"], f(s["pnl"], "+.2f"), f(s["wo1"], "+.2f"), f(s["avg"], "+.1f", "%")]
    body.append(["**all leaders**"] + tot)
    wo3 = []
    for per in ("before", "after"):
        s = pnl_stats([r for r in analysis(paper_rows) if r["period"] == per])
        wo3.append(f"{per}: {f(s['wo3'], '+.2f')} SOL")
    hdr = ["leader", "n", "PnL SOL", "w/o best", "avg %", "n", "PnL SOL", "w/o best", "avg %"]
    t = md_table(hdr, body)
    t = t.replace("| leader |", f"| leader (before #{REF_ID} \\| after #{REF_ID}) |", 1)
    return t + f"\n\nAll leaders without the best 3 trades: {'; '.join(wo3)}. `w/o best` = PnL without that leader's best trade."


def table_evm(paper_rows) -> str:
    body = []
    for chain in ("robinhood", "bsc"):
        for per in ("before", "after"):
            rs = [r for r in paper_rows if r["chain"] == chain and r["period"] == per and r["profile"] == "no"]
            if rs:
                s = pnl_stats(rs)
                body.append((chain, per, s["n"], f"{s['wins']}/{s['n']}", f(s["pnl"], "+.0f"), f(s["wo1"], "+.0f"),
                             f(s["med"], "+.1f", "%")))
    return md_table(["chain", f"vs #{REF_ID}", "n", "wins", "PnL USD", "w/o best", "median %"], body,
                    ["l", "l", "r", "r", "r", "r", "r"])


# ----------------------------------------------------------------------------- exit arms + runner anatomy
def load_params(path: str | None) -> dict:
    p = {"paper_fee": 0.01, "delay": 0.8, "fees": {"pumpfun_curve": 125, "pumpswap_amm": 120, "meteora_damm_v2": 100}}
    try:
        sc = yaml.safe_load(open(os.path.expanduser(path)))["chains"]["solana"]
    except (OSError, TypeError, KeyError, AttributeError):
        return p
    p["paper_fee"] = float(sc.get("paper_fee_per_tx_sol", p["paper_fee"]))
    p["delay"] = float(sc.get("paper_land_delay_s", p["delay"]))
    for k, key in (("pumpfun_curve", "pumpfun_fee_bps"), ("pumpswap_amm", "pumpswap_fee_bps"),
                   ("meteora_damm_v2", "meteora_damm_v2_fee_bps")):
        p["fees"][k] = int(sc.get(key, p["fees"][k]))
    return p


def replay_set(paper_path: str, lmap, params, lo, hi=None):
    """S1 trades (pool-priced, no CUT leaders, flagged / profile out, downtime out) with their tick series."""
    rev = {v: k for k, v in lmap.items() if not k.startswith("0x")}
    exclude = tuple(rev[n] for n in S1_EXCLUDE if n in rev)
    data, _skipped, _mc, _excl, dropped = W.load(f"file:{os.path.abspath(os.path.expanduser(paper_path))}?mode=ro", lo,
                                                 params["fees"], params["paper_fee"], 7 * 86400, pool_only=True,
                                                 exclude_profiles=True, exclude_leaders=exclude, to_id=hi)
    db = open_ro(paper_path)
    down = downtime_ids(db)
    db.close()
    return [p for p in data if p.pid not in down], dropped


def arm_rows(data, params, name_of) -> list[dict]:
    out = []
    for arm, spec in W.ARMS.items():
        pnl, cens = {}, {}
        for p in data:
            v, c = W.run_arm(p, spec, params["delay"], params["paper_fee"], "entry_price")
            (cens if c else pnl)[p.pid] = v - p.cost
        cost = {p.pid: p.cost for p in data}
        v = sorted(pnl.values(), reverse=True)
        pct = sorted(pnl[i] / cost[i] for i in pnl)
        out.append({"arm": arm, "n": len(v), "total": sum(v), "wo1": sum(v[1:]) if len(v) > 1 else None,
                    "wo3": sum(v[3:]) if len(v) > 3 else None, "wins": sum(x > 0 for x in v),
                    "median": pct[len(pct) // 2] if pct else None,
                    "dd": W.max_drawdown([pnl[i] for i in sorted(pnl)]), "cens": len(cens),
                    "cens_mark": sum(cens.values()), "desc": spec[2]})
    return out


def exit_arms(paper_path, lmap, params):
    """{(set, range): (n trades replayed, [arm rows])} for S1 / S2, in-sample (#230-#519) and forward (#520+)."""
    s2 = {k for k, v in lmap.items() if v in S2_LEADERS and not k.startswith("0x")}
    res = {}
    for rng, lo, hi in (("in-sample", H5_FROM, H11_FROM - 1), ("forward", H11_FROM, None)):
        data, _ = replay_set(paper_path, lmap, params, lo, hi)
        res[("S1", rng)] = (len(data), arm_rows(data, params, None))
        sub = [p for p in data if p.leader in s2]
        res[("S2", rng)] = (len(sub), arm_rows(sub, params, None))
    return res


def table_arms(arms) -> str:
    parts = []
    for (sset, rng), (n, rows) in arms.items():
        body = [(r["arm"], r["n"], f(r["total"], "+.2f"), f(r["wo1"], "+.2f"), f(r["wo3"], "+.2f"), f"{r['wins']}/{r['n']}",
                 f(100 * r["median"], "+.1f", "%") if r["median"] is not None else "-", f(r["dd"], "+.2f"),
                 f"{r['cens']} ({f(r['cens_mark'], '+.2f')})") for r in rows]
        note = "designed on these trades: not evidence" if rng == "in-sample" else "trades after the arms were registered (H11)"
        parts.append(f"**{sset}, {rng}** (ids {'%d-%d' % (H5_FROM, H11_FROM - 1) if rng == 'in-sample' else '%d+' % H11_FROM}; "
                     f"{n} replayable trades; {note})\n\n"
                     + md_table(["arm", "n", "total SOL", "w/o best 1", "w/o best 3", "wins", "median", "max DD", "censored (mark SOL)"],
                                body))
    desc = "\n".join(f"- `{r['arm']}` = {r['desc']}" for r in next(iter(arms.values()))[1])
    return "\n\n".join(parts) + "\n\n" + desc


def wilson(k, n, z=1.96):
    if not n:
        return 0.0, 0.0
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return max(0.0, c - h), min(1.0, c + h)


def fmt_t(s):
    if s is None:
        return "-"
    return f"{s:.0f} s" if s < 120 else (f"{s / 60:.0f} min" if s < 7200 else f"{s / 3600:.1f} h")


def price_path(p):
    t0 = p.series[0][0]
    pts = []
    for ts, st, fee, k in p.series:
        if ts - t0 > WINDOW_S:
            break
        pts.append((ts - t0, W.marginal_mult(p, st, fee, k, p.entry_net)))
    return pts


def runner_anatomy(data) -> str:
    """Price path = marginal multiple vs OUR entry (shadow pool after fee), own ticks, clipped 24 h after the fill."""
    paths = [price_path(p) for p in data]
    paths = [x for x in paths if x]
    n = len(paths)
    if not n:
        return "No replayable trades."
    first = lambda pts, lv: next((t for t, m in pts if m >= lv), None)
    peaks = [max(m for _, m in pts) for pts in paths]
    cover = [(p.series[-1][0] - p.series[0][0]) / 3600 for p in data if price_path(p)]
    lv_rows = []
    for lv in LEVELS:
        k = sum(pk >= lv for pk in peaks)
        lo, hi = wilson(k, n)
        lv_rows.append((f"{lv:g}x", f"{k}/{n}", f"{100 * k / n:.0f}%", f"{100 * lo:.0f}-{100 * hi:.0f}%"))
    run = [pts for pts, pk in zip(paths, peaks) if pk >= 3.0]
    non = [pts for pts, pk in zip(paths, peaks) if pk < 3.0]

    def pullback_and_giveback(pts):
        m = [x[1] for x in pts]
        ip = m.index(max(m))
        hi, dd = 0.0, 0.0
        for v in m[:ip + 1]:
            hi = max(hi, v)
            dd = max(dd, 1 - v / hi)
        gain, tp = m[ip] - 1, pts[ip][0]
        give = {}
        for frac in (0.3, 0.5):
            lvl = m[ip] - frac * gain
            give[frac] = next((t - tp for t, v in pts[ip:] if v <= lvl), None)
        return dd, give, tp

    stats = [pullback_and_giveback(pts) for pts in run]
    below = [next((t for t, m in pts if m <= 0.8), None) for pts in non]
    touched = [pts for pts in non if first(pts, 1.3) is not None]
    fell = [next((t for t, m in pts if m <= 0.8), None) for pts in touched]
    rows = [("replayable trades (S1, ids %d+)" % H5_FROM, n),
            ("tick coverage after the fill, median", f"{statistics.median(cover):.1f} h"),
            ("trades with >= 24 h of ticks", f"{sum(c >= 24 for c in cover)}/{n}"),
            ("runners (>= 3x within 24 h)", f"{len(run)}/{n}")]
    if run:
        rows += [("runners: time to 1.3x / 2x / 3x, median",
                  " / ".join(fmt_t(med([first(pts, lv) for pts in run])) for lv in (1.3, 2.0, 3.0))),
                 ("runners: time to the peak, median", fmt_t(med([s[2] for s in stats]))),
                 ("runners: pullback from the running high before the peak, median", f"{-100 * med([s[0] for s in stats]):.0f}%"),
                 ("runners: share with a > 30% / > 50% pullback before the peak",
                  f"{sum(s[0] > 0.3 for s in stats)}/{len(run)} / {sum(s[0] > 0.5 for s in stats)}/{len(run)}"),
                 ("runners: time until 30% / 50% of the gain is given back after the peak, median",
                  f"{fmt_t(med([s[1][0.3] for s in stats]))} / {fmt_t(med([s[1][0.5] for s in stats]))}")]
    if non:
        rows += [("non-runners: touched 1.3x", f"{len(touched)}/{len(non)}"),
                 ("non-runners that touched 1.3x and later fell to <= 0.8x", f"{sum(x is not None for x in fell)}/{len(touched)}"),
                 ("non-runners: time to <= 0.8x, median", fmt_t(med(below))),
                 ("non-runners: multiple at the end of the window, median", f"{statistics.median([pts[-1][1] for pts in non]):.2f}x")]
    return (md_table(["peak multiple of our entry (24 h)", "trades", "share", "95% interval"], lv_rows) + "\n\n"
            + md_table(["measure", "value"], rows, ["l", "r"]))


# ----------------------------------------------------------------------------- charts
def style_axes(ax):
    ax.set_facecolor(SURFACE)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(GRID)
    ax.tick_params(colors=INK2, labelsize=8)
    ax.grid(True, color=GRID, linewidth=0.6)
    ax.set_axisbelow(True)
    ax.xaxis.label.set_color(INK2)
    ax.yaxis.label.set_color(INK2)


def make_charts(outdir, paper_rows, live_rows, cmp_rows, arms) -> list[tuple[str, str]]:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    os.makedirs(outdir, exist_ok=True)
    made = []

    def save(fig, name, title):
        fig.patch.set_facecolor(SURFACE)
        fig.savefig(os.path.join(outdir, name), dpi=130, facecolor=SURFACE, metadata={"Software": "matplotlib"})
        plt.close(fig)
        made.append((name, title))

    # 1. PnL per leader, before vs after
    res = leader_results(paper_rows)
    names = [n for n in sorted(res, key=leader_key) if res[n]["before"] or res[n]["after"]][::-1]
    fig, ax = plt.subplots(figsize=(7.5, max(4, 0.32 * len(names) + 1.5)))
    ys = range(len(names))
    ax.barh([y + 0.2 for y in ys], [pnl_stats(res[n]["before"])["pnl"] if res[n]["before"] else 0 for n in names], 0.38,
            color=C1, label=f"before #{REF_ID}")
    ax.barh([y - 0.2 for y in ys], [pnl_stats(res[n]["after"])["pnl"] if res[n]["after"] else 0 for n in names], 0.38,
            color=C2, label=f"from #{REF_ID}")
    ax.set_yticks(list(ys)); ax.set_yticklabels(names)
    ax.axvline(0, color=INK2, linewidth=0.8)
    ax.set_xlabel("paper PnL (SOL), Solana, flagged and profile positions out")
    ax.set_title("PnL per leader, before vs after #%d" % REF_ID, loc="left", color=INK, fontsize=11)
    ax.legend(frameon=False, fontsize=8, loc="lower right", labelcolor=INK2)
    style_axes(ax); ax.grid(axis="y", visible=False)
    fig.tight_layout()
    save(fig, "pnl_per_leader.png", "PnL per leader, before vs after #%d" % REF_ID)

    # 2. entry gap vs PnL per trade
    fig, ax = plt.subplots(figsize=(7.5, 4.8))
    clipped = 0
    for rows, color, label in ((analysis(paper_rows), C1, "paper"), (analysis(live_rows), C2, "live")):
        pts = [(r["entry_gap_pct"], r["pnl_pct"]) for r in rows if r["entry_gap_pct"] is not None and r["pnl_pct"] is not None]
        inside = [(x, y) for x, y in pts if -60 <= x <= 200]
        clipped += len(pts) - len(inside)
        ax.scatter([x for x, _ in inside], [y for _, y in inside], s=14, color=color, alpha=0.6, linewidths=0.5,
                   edgecolors=SURFACE, label=f"{label} (n={len(inside)})")
    ax.axhline(0, color=INK2, linewidth=0.8); ax.axvline(0, color=INK2, linewidth=0.8)
    ax.set_xlabel("entry gap vs leader's price (%), + = we paid more")
    ax.set_ylabel("return on cost (%)")
    ax.set_title("Entry gap vs return per trade" + (f" ({clipped} outside x-range not shown)" if clipped else ""),
                 loc="left", color=INK, fontsize=11)
    ax.legend(frameon=False, fontsize=8, labelcolor=INK2)
    style_axes(ax); fig.tight_layout()
    save(fig, "entry_gap_vs_pnl.png", "Entry gap vs return per trade")

    # 3. pool vs Jupiter cumulative PnL (paper, from H5_FROM)
    fig, ax = plt.subplots(figsize=(7.5, 4.4))
    base = [r for r in analysis(paper_rows) if r["id"] >= H5_FROM]
    for label, venues, color in (("pool (curve + pumpswap)", ("curve", "pumpswap"), C1), ("jupiter", ("jupiter",), C2),
                                 ("vault (Meteora)", ("vault",), C3)):
        rs = sorted((r for r in base if r["venue"] in venues), key=lambda r: r["id"])
        if not rs:
            continue
        cum, tot = [], 0.0
        for r in rs:
            tot += r["pnl"]; cum.append(tot)
        ax.step([r["id"] for r in rs], cum, where="post", color=color, linewidth=1.6, label=f"{label} (n={len(rs)}, {cum[-1]:+.2f})")
    ax.axhline(0, color=INK2, linewidth=0.8)
    for x, lbl in ((REF_ID, f"#{REF_ID}"), (H11_FROM, f"#{H11_FROM} corrected PumpSwap prices")):
        ax.axvline(x, color=INK2, linewidth=0.8, linestyle=":")
        ax.text(x, ax.get_ylim()[1], " " + lbl, fontsize=7, color=INK2, va="top")
    ax.set_xlabel("paper position id"); ax.set_ylabel("cumulative PnL (SOL)")
    ax.set_title(f"Pool-priced vs Jupiter-priced positions, cumulative PnL (ids {H5_FROM}+)", loc="left", color=INK, fontsize=11)
    ax.legend(frameon=False, fontsize=8, loc="upper left", bbox_to_anchor=(0, 0.93), labelcolor=INK2)
    style_axes(ax); fig.tight_layout()
    save(fig, "pool_vs_jupiter_cumulative.png", "Pool vs Jupiter cumulative PnL")

    # 4. live vs paper per trade (dumbbell)
    rows = [r for r in cmp_rows if r["live_ret"] is not None and r["paper_ret"] is not None]
    fig, ax = plt.subplots(figsize=(7.5, max(3.5, 0.18 * len(rows) + 1.6)))
    ys = list(range(len(rows)))[::-1]
    for y, r in zip(ys, rows):
        ax.plot([r["paper_ret"], r["live_ret"]], [y, y], color=GRID, linewidth=1.5, zorder=1)
    ax.scatter([r["paper_ret"] for r in rows], ys, s=22, color=C1, zorder=2, label="paper", edgecolors=SURFACE, linewidths=0.6)
    ax.scatter([r["live_ret"] for r in rows], ys, s=22, color=C2, zorder=3, label="live", edgecolors=SURFACE, linewidths=0.6)
    ax.set_yticks(ys); ax.set_yticklabels([f"live #{r['id']}" for r in rows], fontsize=6)
    h8 = [y for y, r in zip(ys, rows) if r["id"] >= H8_FIRST_LIVE_ID]
    if h8:
        ax.axhspan(min(h8) - 0.5, max(h8) + 0.5, color=GRID, alpha=0.45, zorder=0)
        ax.text(ax.get_xlim()[1], max(h8), "H8-LIVE ", fontsize=7, color=INK2, ha="right", va="center")
    ax.axvline(0, color=INK2, linewidth=0.8)
    ax.set_xlabel("return on cost (%)")
    ax.set_title("Live vs paper, same leader signal, per trade", loc="left", color=INK, fontsize=11)
    ax.legend(frameon=False, fontsize=8, loc="lower right", labelcolor=INK2)
    style_axes(ax); ax.grid(axis="y", visible=False); fig.tight_layout()
    save(fig, "live_vs_paper_per_trade.png", "Live vs paper per trade")

    # 5. exit arms (S1)
    panels = [(k, v) for k, v in arms.items() if k[0] == "S1"]
    fig, axes = plt.subplots(1, max(1, len(panels)), figsize=(7.5, 3.8), sharey=True, squeeze=False)
    for ax, ((sset, rng), (n, rows)) in zip(axes[0], panels):
        xs = range(len(rows))
        ax.bar([x - 0.2 for x in xs], [r["total"] for r in rows], 0.38, color=C1, label="total")
        ax.bar([x + 0.2 for x in xs], [r["wo3"] or 0 for r in rows], 0.38, color=C2, label="without best 3")
        ax.set_xticks(list(xs)); ax.set_xticklabels([r["arm"] for r in rows])
        ax.axhline(0, color=INK2, linewidth=0.8)
        ax.set_title(f"{sset} {rng} ({n} trades)", loc="left", color=INK, fontsize=9)
        style_axes(ax); ax.grid(axis="x", visible=False)
    axes[0][0].set_ylabel("PnL (SOL)")
    axes[0][0].legend(frameon=False, fontsize=8, labelcolor=INK2)
    fig.tight_layout()
    save(fig, "exit_arms.png", "Exit arms on corrected prices")
    return made


# ----------------------------------------------------------------------------- leak scan
def leak_scan(outdir: str, dbs: list[sqlite3.Connection]) -> list[str]:
    """-> findings. Text outputs: address patterns, and any 8-char window equal to the prefix of a leader / token /
    signature string stored in the databases."""
    prefixes = set()
    for db in dbs:
        for q in ("SELECT token FROM positions", "SELECT leader FROM positions", "SELECT token FROM signals",
                  "SELECT leader FROM signals", "SELECT entry_sig FROM positions", "SELECT exit_sig FROM positions"):
            try:
                prefixes |= {r[0][:8] for r in db.execute(q) if r[0] and len(r[0]) >= 32}
            except sqlite3.OperationalError:
                pass
    found = []
    for root, _, files in os.walk(outdir):
        for fn in files:
            if not fn.endswith((".csv", ".md")):
                continue
            path = os.path.join(root, fn)
            text = open(path).read()
            for m in ADDR_RE.finditer(text):
                found.append(f"{path}: address pattern at offset {m.start()}")
            for m in re.finditer(r"[A-Za-z0-9]{8,}", text):
                run = m.group()
                if any(run[i:i + 8] in prefixes for i in range(len(run) - 7)):
                    found.append(f"{path}: 8-char prefix of a database address at offset {m.start()}")
    return found


# ----------------------------------------------------------------------------- RESULTS.md
def flag_summary(rows) -> str:
    c = defaultdict(int)
    for r in rows:
        if r["flagged"] == "yes":
            for reason in r["flag_reason"].split("+"):
                c[reason] += 1
    n = sum(r["flagged"] == "yes" for r in rows)
    return f"{n}" + (" (" + ", ".join(f"{k} {v}" for k, v in sorted(c.items())) + ")" if n else "")


def results_md(parts: dict, rng: dict, charts) -> str:
    img = {n: t for n, t in charts}
    def chart(name):
        return f"![{img[name]}](charts/{name})" if name in img else ""
    return f"""# Results

Generated by `scripts/export_results.py` from the paper (dev) and live databases, read-only. Facts and definitions only.

Data: paper positions opened {rng['paper'][0]} to {rng['paper'][1]} (UTC dates), live positions {rng['live'][0]} to {rng['live'][1]}.
Per-position data: [`positions_paper.csv`](positions_paper.csv), [`positions_live.csv`](positions_live.csv).

## Definitions and filters

- **Period**: before / after paper position #{REF_ID} (restart of 2026-10-08). Live positions carry their own ids, so they are placed by opening time against the opening time of paper #{REF_ID}.
- **Venue class**: `curve` = pump.fun bonding curve, `pumpswap` = PumpSwap AMM, `jupiter` = priced by Jupiter quotes, `vault` = Meteora (vault balances x Jupiter factor), `evm` = Robinhood / BSC, `unknown` = no venue logged at the fill. *pool* = curve + pumpswap.
- **Entry gap**: our fill price (cost / tokens, all-in) vs the leader's price in the swap we copied, in % (+ = we paid more). EVM: median of the position's buy-fill gaps.
- **Entry market cap bucket** (USD at the fill, as logged by the bot): <30k, 30-100k, 100-250k, 250-500k, >=500k, n/a.
- **Flagged** (CSV column `flagged`, `flag_reason`): excluded from every table and chart (closed positions flagged: paper {rng['flags'][0]}, live {rng['flags'][1]}). `mark_step` = single-step mark change > 50% not confirmed by an independent price; `bot_flag` = flagged by the bot itself; `downtime` = open while the dev bot was down (2026-10-10 20:35 to 2026-10-11 12:41 UTC, exits not monitored).
- **Profile positions** (`profile` = yes) were opened under a per-leader profile with their own exit rules; excluded from the Solana tables, kept in the CSV.
- Solana PnL is in SOL; EVM PnL in USD. The tables below use closed Solana paper positions that are not flagged and not profile positions, unless a heading says otherwise.
- PumpSwap prices include `virtual_quote_reserves` in the dev pool model from position #{H11_FROM} on. Paper positions before that were priced and filled on the vault-only model. The exit-arm replays and the runner anatomy re-price every PumpSwap tick on vault + reconstructed virtual reserves ("corrected prices"); the CSV and the H5 / leader tables are the results as recorded.

## H5: venue split, pool vs Jupiter (paper, from #{H5_FROM})

{parts['h5']}

{chart('pool_vs_jupiter_cumulative.png')}

## H8-LIVE: live vs paper

{parts['h8']}

{chart('live_vs_paper_per_trade.png')}

Entry gap and per-trade return, all Solana positions with a logged leader price:

{chart('entry_gap_vs_pnl.png')}

## Leaders, before vs after #{REF_ID} (paper)

{parts['leaders']}

{chart('pnl_per_leader.png')}

## EVM positions (paper, USD, profile positions out)

{parts['evm']}

## Exit arms on corrected prices (what-if replays on recorded ticks)

Pool-priced S1 trades (all Solana leaders except {', '.join(S1_EXCLUDE)}); S2 = {', '.join(S2_LEADERS)}. Fills on the first tick at or after the trigger + the paper landing delay, on the shadow pool (our own buy added), pool fee + paper fee per sell, stop basis entry price. Censored = the data ended while still holding; never counted as a win or a loss.

{parts['arms']}

{chart('exit_arms.png')}

## Runner anatomy (S1, ids {H5_FROM}+, corrected prices)

{parts['runners']}

## Files

- `positions_paper.csv`, `positions_live.csv`: columns {', '.join(f'`{c}`' for c in COLUMNS)}. Dates are UTC days; `hold_s` is the hold time in seconds; `cost` and `pnl` in `unit`.
- `tables/*.md`: the tables above. `charts/*.png`: the charts above.
"""


# ----------------------------------------------------------------------------- main
def export_all(paper_path, live_path, lmap, out, config=None) -> list[str]:
    """Writes `out`; returns leak-scan findings (then nothing public is left behind: the output folder is removed)."""
    import shutil
    paper, live = open_ro(paper_path), open_ro(live_path)
    ref_ts = paper.execute("SELECT opened_at FROM positions WHERE id=?", (REF_ID,)).fetchone()
    ref_ts = ref_ts[0] if ref_ts else 0.0
    paper_rows = collect_positions(paper, lmap, ref_ts=ref_ts, paper=True)
    live_rows = collect_positions(live, lmap, ref_ts=ref_ts, paper=False, downtime=False)
    cmp_rows, _ = LP.compare(LP.open_ro(live_path), LP.open_ro(paper_path), 0, 120.0)
    bad_live = {r["id"] for r in live_rows if r["flagged"] == "yes"}
    bad_paper = {r["id"] for r in paper_rows if r["flagged"] == "yes"}
    cmp_rows = [r for r in cmp_rows if r["id"] not in bad_live and r.get("paper_id") not in bad_paper]
    _, s8 = LP.compare(LP.open_ro(live_path), LP.open_ro(paper_path), H8_FIRST_LIVE_ID, 120.0)
    cmp_h8 = [r for r in cmp_rows if r["id"] >= H8_FIRST_LIVE_ID]
    for r in cmp_rows:
        r["_leader"] = pseudonym(r["leader"], lmap)
        r.pop("token", None); r.pop("leader", None)
    params = load_params(config)
    arms = exit_arms(paper_path, lmap, params)
    s1, _ = replay_set(paper_path, lmap, params, H5_FROM)
    parts = {"h5": table_h5(paper_rows), "h8": table_h8(live_rows, paper_rows, cmp_h8, s8),
             "leaders": table_leaders(paper_rows), "evm": table_evm(paper_rows), "arms": table_arms(arms),
             "runners": runner_anatomy(s1)}
    dates = lambda rows: (min(r["opened_date"] for r in rows), max(r["opened_date"] for r in rows)) if rows else ("-", "-")
    tmp = out.rstrip("/") + ".tmp"
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(os.path.join(tmp, "tables"))
    write_csv(os.path.join(tmp, "positions_paper.csv"), paper_rows)
    write_csv(os.path.join(tmp, "positions_live.csv"), live_rows)
    for name, key in (("h5_venue_split", "h5"), ("h8_live_vs_paper", "h8"), ("leaders_before_after", "leaders"),
                      ("evm_summary", "evm"), ("exit_arms", "arms"), ("runner_anatomy", "runners")):
        open(os.path.join(tmp, "tables", name + ".md"), "w").write(parts[key] + "\n")
    charts = make_charts(os.path.join(tmp, "charts"), paper_rows, live_rows, cmp_rows, arms)
    open(os.path.join(tmp, "RESULTS.md"), "w").write(
        results_md(parts, {"paper": dates(paper_rows), "live": dates(live_rows),
                                    "flags": (flag_summary(paper_rows), flag_summary(live_rows))}, charts))
    findings = leak_scan(tmp, [paper, live])
    if findings:
        shutil.rmtree(tmp, ignore_errors=True)
        return findings
    shutil.rmtree(out, ignore_errors=True)
    os.rename(tmp, out)
    return []


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--paper", default="copybot.db")
    ap.add_argument("--live", default="~/Documents/copybot-live/copybot.db")
    ap.add_argument("--leaders-map", default="leaders_map.local.txt")
    ap.add_argument("--out", default="results")
    ap.add_argument("-c", "--config", default="config.yaml", help="only paper fee, landing delay and pool fees are read")
    a = ap.parse_args()
    findings = export_all(a.paper, a.live, load_leader_map(a.leaders_map), a.out, a.config)
    if findings:
        print("LEAK SCAN FAILED, nothing written:\n  " + "\n  ".join(findings[:20]))
        sys.exit(1)
    print(f"wrote {a.out}/ (leak scan clean)")


if __name__ == "__main__":
    main()
