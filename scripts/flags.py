"""Data-quality flags for positions (read-only; shared by the reports).

DATA RULE (CHECKLIST_30_TRADES.md): a position whose marks show a single-step change > 50% that is NOT confirmed
by an independent price source is excluded from every test, winners and losers alike. Known cases (Oct 8 2026):
299 (curve quoted in a non-SOL token, graduation -> Jupiter 0.07), 192 (frozen curve state for 10 h, then
Jupiter -60%), 269 (Jupiter +124% in one poll, take-profit), 40 (-59% in one step after 39 h).

Rule, as implemented in flagged_positions():
  * only Solana positions (their marks come from the bot's pool model or single Jupiter quotes); xchain
    positions are marked with Relay quotes, which are market quotes themselves -> never flagged by this rule
  * a step = two consecutive rows of `marks` for the position; change = |v2 / v1 - 1| > threshold (0.5)
  * confirmed = a `pool_factors` row for the token within confirm_s (600 s) AFTER the step with a factor in
    0.67-1.5 (the bot measured Jupiter against the pool and they agreed), OR the step is the first mark after
    an add-on buy (timing["adds"] - the value legitimately jumps by the add)
  * plus every position the bot itself flagged live (timing["flagged"], source-switch guard, bot/positions.py)
"""
from __future__ import annotations

import json
import sqlite3

THRESHOLD = 0.5
CONFIRM_S = 600.0
FACTOR_OK = (0.67, 1.5)


def flagged_positions(db: sqlite3.Connection, threshold: float = THRESHOLD, confirm_s: float = CONFIRM_S,
                      chain: str = "solana") -> dict[int, str]:
    """-> {position id: reason} for every flagged position of `chain` (paper and live)."""
    out: dict[int, str] = {}
    pos = {}
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "positions" not in tables or "marks" not in tables:          # minimal test databases
        return out
    for pid, token, timing in db.execute("SELECT id, token, timing FROM positions WHERE chain=?", (chain,)):
        t = json.loads(timing or "{}")
        pos[pid] = (token, t)
        if t.get("flagged"):
            out[pid] = f"bot: {t['flagged']}"
    factors: dict[str, list[tuple[float, float]]] = {}
    try:
        for token, ts, f in db.execute("SELECT token, ts, factor FROM pool_factors ORDER BY ts"):
            factors.setdefault(token, []).append((ts, f))
    except sqlite3.OperationalError:
        pass
    last: dict[int, tuple[float, float]] = {}
    worst: dict[int, tuple[float, float, float, float]] = {}       # pid -> (change, ts, v1, v2) of the worst step
    for pid, ts, v in db.execute("SELECT position_id, ts, value FROM marks ORDER BY position_id, ts"):
        if pid not in pos:
            continue
        prev = last.get(pid)
        last[pid] = (ts, v)
        if not prev or not prev[1] or v is None:
            continue
        change = abs(v / prev[1] - 1)
        if change > threshold and (pid not in worst or change > worst[pid][0]):
            if _confirmed(pos[pid], prev[0], ts, factors, confirm_s):
                continue
            worst[pid] = (change, ts, prev[1], v)
    for pid, (change, ts, v1, v2) in worst.items():
        if pid not in out:
            out[pid] = f"single-step mark change {100 * (v2 / v1 - 1):+.0f}% at {ts:.0f} ({v1:.4g} -> {v2:.4g}), unconfirmed"
    return out


def _confirmed(pos, t1, t2, factors, confirm_s) -> bool:
    token, timing = pos
    for ts, f in factors.get(token, ()):
        if t2 <= ts <= t2 + confirm_s and FACTOR_OK[0] <= f <= FACTOR_OK[1]:
            return True
    for add_ts in timing.get("add_ts") or []:                      # Solana profile adds (bot/main.py)
        if t1 <= add_ts <= t2:
            return True
    return False


def flagged_note(flagged: dict[int, str], shown: bool) -> str:
    if not flagged:
        return "flagged positions: none"
    ids = ", ".join(str(i) for i in sorted(flagged))
    return (f"flagged positions {'INCLUDED' if shown else 'excluded'} ({len(flagged)}: {ids})"
            + ("" if shown else " - data rule: single-step mark change > 50% unconfirmed; --include-flagged shows them"))
