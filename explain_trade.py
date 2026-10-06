"""Explain one closed paper trade: what the price did before, during and after it.

Usage (from the copybot folder):
    python explain_trade.py <token address or position id>

All market caps are on the BOT's scale. For some PumpSwap pools that scale is 0-22% below
DexScreener, so compare percentages with the chart, not absolute market caps.
"""
import json
import sqlite3
import sys
from datetime import datetime

import yaml

from bot.pricing.pools import PoolState
from whatif_ticksv2 import sell, L

arg = sys.argv[1] if len(sys.argv) > 1 else None
if not arg:
    print(__doc__); sys.exit(1)
db = sqlite3.connect("copybot.db")
where = "id=?" if arg.isdigit() else "token=?"
row = db.execute(f"SELECT id, token, leader, token_amount, cost, exit_value, exit_reason, opened_at, closed_at, "
                 f"timing FROM positions WHERE {where} AND status='closed' ORDER BY id DESC LIMIT 1", (arg,)).fetchone()
if not row:
    print("No closed position found for that token / id."); sys.exit(1)
pid, token, leader, amount, cost, exitv, reason, opened, closed, timing = row
t = json.loads(timing or "{}")
try:
    sc = yaml.safe_load(open("config.yaml"))["chains"]["solana"]
except Exception:
    sc = {}
fees = {"pumpfun_curve": int(sc.get("pumpfun_fee_bps", 125)), "pumpswap_amm": int(sc.get("pumpswap_fee_bps", 100))}
paper_fee = float(sc.get("paper_fee_per_tx_sol", 0.01))

r = db.execute("SELECT reason FROM signals WHERE token=? AND leader=? AND action='bought' ORDER BY ABS(ts-?) LIMIT 1",
               (token, leader, opened)).fetchone()
mc_check = None
if r and r[0] and "mc $" in r[0]:
    mc_check = float(r[0].split("mc $")[1].split()[0].replace(",", ""))

start = t.get("checked_at", opened) - 1
ticks = db.execute("SELECT ts, venue, tok_res, sol_res, real_tok, real_sol FROM ticks WHERE token=? AND ts>=? "
                   "ORDER BY ts", (token, start)).fetchall()
if not ticks:
    print("No price data recorded for this token (not a pool-priced venue?)."); sys.exit(1)
states = [(ts, PoolState(v, int(a), int(b), None if c is None else int(c), None if d is None else int(d)), fees.get(v, 0))
          for ts, v, a, b, c, d in ticks]
px = lambda st: st.sol_res / st.tok_res
def at(ts):
    before = [s for s in states if s[0] <= ts]
    return before[-1] if before else states[0]
p0 = px(at(t.get("checked_at", opened))[1])
mc = lambda p: f"${mc_check * p / p0:,.0f}" if mc_check else f"{p / p0:.3f}x"
hhmm = lambda ts: datetime.fromtimestamp(ts).strftime("%H:%M:%S")
entry_net = int((cost - paper_fee) * L)

def value(s):          # what selling the whole position would give at state s (shadow pool)
    ts, st, f = s
    net = entry_net * (10_000 - f) // 10_000
    return sell(st, f, int(amount), net)[0] / L - paper_fee

fill = at(t.get("filled_at", opened))
during = [s for s in states if fill[0] <= s[0] <= closed]
after = [s for s in states if s[0] > closed]
low = min(during, key=lambda s: px(s[1])) if during else fill
high = max(during, key=lambda s: px(s[1])) if during else fill

print(f"position {pid}  {token}  ({reason}, {exitv / cost - 1:+.1%})")
print(f"market caps on the bot's scale; compare % with the chart\n")
print(f"  {hhmm(t.get('checked_at', opened))}  safety check      {mc(p0)}")
print(f"  {hhmm(fill[0])}  your fill         {mc(px(fill[1]))}   ({px(fill[1]) / p0 - 1:+.1%} vs check)")
print(f"  {hhmm(high[0])}  high while held   {mc(px(high[1]))}   ({px(high[1]) / px(fill[1]) - 1:+.1%} vs fill)")
print(f"  {hhmm(low[0])}  low while held    {mc(px(low[1]))}   ({px(low[1]) / px(fill[1]) - 1:+.1%} vs fill)")
if t.get("exit_trigger_at"):
    trig = at(t["exit_trigger_at"])
    print(f"  {hhmm(trig[0])}  stop/TP triggered {mc(px(trig[1]))}   ({px(trig[1]) / px(fill[1]) - 1:+.1%} vs fill)")
print(f"  {hhmm(closed)}  sold              result {exitv - cost:+.3f} SOL ({exitv / cost - 1:+.1%})")
print(f"\n  entry costs: first value after buying was {value(fill) / cost - 1:+.1%} vs cost "
      f"(fees both ways + price impact + paper fees)")
if after:
    peak = max(after, key=lambda s: px(s[1]))
    end = after[-1]
    print(f"\nafter you sold (recorded until {hhmm(end[0])}):")
    print(f"  peak  {hhmm(peak[0])}  {mc(px(peak[1]))}   holding would have been worth {value(peak) / cost - 1:+.1%}")
    print(f"  last  {hhmm(end[0])}  {mc(px(end[1]))}   holding would have been worth {value(end) / cost - 1:+.1%}")
else:
    print("\nno price data after the sale (tail not recorded)")
