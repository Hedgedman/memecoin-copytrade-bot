"""Why did (or didn't) the bot trade this token / what did this leader do?

Usage (from the bot folder):
    python why.py <token address>     everything the bot recorded about a token
    python why.py <leader wallet>     everything the bot recorded from a leader (last 24 h)

Sources: signals (what the bot decided), leader_events (leader txs that never became a
signal: small_buy, not_a_swap, not_signer, tx_not_found), positions (what it traded),
xchain_fills (paper fills of cross-chain positions: his price vs our Relay quote; amounts in USD).
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
unit = lambda chain: "SOL" if chain in (None, "solana") else "USD"   # xchain signals/positions are in USD
has_events = db.execute("SELECT name FROM sqlite_master WHERE name='leader_events'").fetchone()

leader_mode = bool(db.execute("SELECT 1 FROM signals WHERE leader=? LIMIT 1", (key,)).fetchone()) or \
    bool(has_events and db.execute("SELECT 1 FROM leader_events WHERE leader=? LIMIT 1", (key,)).fetchone())

if leader_mode:
    since = __import__("time").time() - 24 * 3600
    print(f"LEADER {key} - last 24 h\n")
    rows = [(ts, f"signal {side:<4} {action:<11} {token[:8]}  {sol or 0:.3f} {unit(chain)}  {reason or ''}")
            for ts, side, action, token, sol, reason, chain in db.execute(
                "SELECT ts, side, action, token, quote_amount, reason, chain FROM signals WHERE leader=? AND ts>?",
                (key, since))]
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
for ts, leader, side, action, reason, sol, chain in db.execute(
        "SELECT ts, leader, side, action, reason, quote_amount, chain FROM signals WHERE token=? ORDER BY ts", (key,)):
    found = True
    print(f"  {t(ts)}  signal {side:<4} by {leader[:8]}  {sol or 0:.3f} {unit(chain)} -> {action}  {reason or ''}")
if has_events:
    for ts, leader, kind, sol, detail in db.execute(
            "SELECT ts, leader, kind, sol, detail FROM leader_events WHERE token=? ORDER BY ts", (key,)):
        found = True
        print(f"  {t(ts)}  DROPPED by {leader[:8]}: {kind}  {'' if sol is None else f'{sol:.3f} SOL'}  {detail or ''}")
for pid, opened, status, reason, cost, exitv, chain in db.execute(
        "SELECT id, opened_at, status, exit_reason, cost, exit_value, chain FROM positions WHERE token=? ORDER BY id",
        (key,)):
    found = True
    res = f"{exitv / cost - 1:+.1%}" if exitv else "open"
    print(f"  {t(opened)}  POSITION {pid} ({chain}): {status} {reason or ''} {res}  cost {cost:.3f} {unit(chain)}")
if db.execute("SELECT name FROM sqlite_master WHERE name='xchain_fills'").fetchone():
    for ts, pid, side, reason, frac, usd, ours, his, gap, delay in db.execute(
            "SELECT ts, position_id, side, reason, fraction, our_usd, our_price_usd, his_price_usd, gap_pct, delay_s "
            "FROM xchain_fills WHERE lower(token)=lower(?) ORDER BY ts", (key,)):
        found = True
        print(f"  {t(ts)}  xFILL pos {pid} {side:<4} {reason:<22} {frac:>4.0%}  ${usd:,.2f}  ours ${ours:.4g} vs his "
              f"{f'${his:.4g}' if his else 'n/a'} ({f'{gap:+.1f}%' if gap is not None else '-'}), "
              f"delay {f'{delay:.0f}s' if delay is not None else '-'}")
if not found:
    print("  nothing recorded for this token.")
    print("  -> the bot never saw a leader trade it: the buyer wasn't a leader (or not yet), the bot")
    print("     wasn't running, or (before this update) a dropped tx wasn't logged. Note: 'not_a_swap'")
    print("     drops have no token; run  python why.py <leader wallet>  to see those.")
