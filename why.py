"""Why did (or didn't) the bot trade this token / what did this leader do?

Usage (from the bot folder):
    python why.py <token address>     everything the bot recorded about a token
    python why.py <leader wallet>     everything the bot recorded from a leader (last 24 h)

Sources: signals (what the bot decided), leader_events (leader txs that never became a
signal: small_buy, not_a_swap, not_signer, tx_not_found), positions (what it traded).
If NOTHING shows up, the bot never saw the transaction: the wallet wasn't a leader
at that moment, or the bot wasn't running.
"""
import sqlite3
import sys
from datetime import datetime

if len(sys.argv) != 2:
    print(__doc__); sys.exit(1)
key = sys.argv[1].strip()
db = sqlite3.connect("copybot.db")
t = lambda ts: datetime.fromtimestamp(ts).strftime("%m-%d %H:%M:%S")
has_events = db.execute("SELECT name FROM sqlite_master WHERE name='leader_events'").fetchone()

leader_mode = bool(db.execute("SELECT 1 FROM signals WHERE leader=? LIMIT 1", (key,)).fetchone()) or \
    bool(has_events and db.execute("SELECT 1 FROM leader_events WHERE leader=? LIMIT 1", (key,)).fetchone())

if leader_mode:
    since = __import__("time").time() - 24 * 3600
    print(f"LEADER {key} - last 24 h\n")
    rows = [(ts, f"signal {side:<4} {action:<11} {token[:8]}  {sol or 0:.3f} SOL  {reason or ''}")
            for ts, side, action, token, sol, reason in db.execute(
                "SELECT ts, side, action, token, quote_amount, reason FROM signals WHERE leader=? AND ts>?", (key, since))]
    if has_events:
        rows += [(ts, f"DROPPED {kind:<12} {(token or '')[:8]}  {'' if sol is None else f'{sol:.3f} SOL'}  {detail or ''}  {sig[:12]}")
                 for ts, kind, token, sol, detail, sig in db.execute(
                     "SELECT ts, kind, token, sol, detail, signature FROM leader_events WHERE leader=? AND ts>?", (key, since))]
    for ts, line in sorted(rows):
        print(f"  {t(ts)}  {line}")
    if not rows:
        print("  nothing recorded: he didn't trade, or the bot wasn't watching him (check leaders + restart time)")
    sys.exit(0)

print(f"TOKEN {key}\n")
found = False
for ts, leader, side, action, reason, sol in db.execute(
        "SELECT ts, leader, side, action, reason, quote_amount FROM signals WHERE token=? ORDER BY ts", (key,)):
    found = True
    print(f"  {t(ts)}  signal {side:<4} by {leader[:8]}  {sol or 0:.3f} SOL -> {action}  {reason or ''}")
if has_events:
    for ts, leader, kind, sol, detail in db.execute(
            "SELECT ts, leader, kind, sol, detail FROM leader_events WHERE token=? ORDER BY ts", (key,)):
        found = True
        print(f"  {t(ts)}  DROPPED by {leader[:8]}: {kind}  {'' if sol is None else f'{sol:.3f} SOL'}  {detail or ''}")
for pid, opened, status, reason, cost, exitv in db.execute(
        "SELECT id, opened_at, status, exit_reason, cost, exit_value FROM positions WHERE token=? ORDER BY id", (key,)):
    found = True
    res = f"{exitv / cost - 1:+.1%}" if exitv else "open"
    print(f"  {t(opened)}  POSITION {pid}: {status} {reason or ''} {res}")
if not found:
    print("  nothing recorded for this token.")
    print("  -> the bot never saw a leader trade it: the buyer wasn't a leader (or not yet), the bot")
    print("     wasn't running, or (before this update) a dropped tx wasn't logged. Note: 'not_a_swap'")
    print("     drops have no token; run  python why.py <leader wallet>  to see those.")
