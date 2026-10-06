# Checks when you have ~30 new trades (count from position 44)

Count trades:
sqlite3 copybot.db "SELECT COUNT(*), ROUND(SUM(exit_value - cost),2) FROM positions WHERE id >= 44 AND status = 'closed';"

1. Exit styles (fixed / trailing / partial) + speed table:
python whatif_ticksv2.py --from-id 44

2. Market cap split:
python whatif_ticksv2.py --from-id 44 --max-mc 500000
python whatif_ticksv2.py --from-id 44 --min-mc 500000

3. How much more you pay than the leader (speed check):
sqlite3 -header -column copybot.db "SELECT p.id, substr(p.token,1,6) AS token, ROUND(((p.cost - 0.01) / CAST(p.token_amount AS REAL)) / (SELECT s.quote_amount / CAST(s.token_amount AS REAL) FROM signals s WHERE s.token = p.token AND s.leader = p.leader AND s.action = 'bought' ORDER BY ABS(s.ts - p.opened_at) LIMIT 1) - 1, 3) AS you_vs_leader, ROUND((p.exit_value - p.cost) / p.cost, 3) AS pnl FROM positions p WHERE p.id >= 44 AND p.status = 'closed' ORDER BY p.id;"

4. Timing per stage:
python timings.py --from-id 44

Notes
- Leave out positions hurt by downtime (e.g. 45, if it was unwatched for long).
- PumpSwap fee changed 30 -> 100 bps at 13:08 on Oct 5.
- Bot market caps can be 0-22% below DexScreener for some PumpSwap pools; PnL % is still correct.
- Paste all outputs into a chat with Claude, together with this file.
- Helius Developer from Oct 05 18:35: tail 6h, 150 pools.
- transactionSubscribe detection since Oct 05 18:40.
- Jupiter paid plan + jupiter_max_rps 7 since Oct 05 21:29.
- Oct 5 evening: Meteora DAMM v2 vault pricing, poll 0.5s, rps 8 (live) / 1 (paper). Running overnight.
HYPOTHESIS 3 (Oct 06 10:53) replaces H2: mc 30k-500k, stop -20%, trailing 30% after +50%. Judge ONLY on trades from ID 31 at 20-30 trades, unchanged.
  + only_first_leader_buy on in live since Oct 06 11:53.
- Manual sell: stop bot, then  SOL_PRIVATE_KEY="$(security find-generic-password -a copybot -s SOL_PRIVATE_KEY -w)" python close_all.py  [--id N]
- Oct 06 23:18: LIVE bot stopped. Dev/paper bot capacity: jupiter_max_rps 9 -> 8 (plan limit 10, live off), poll_interval_s 1.0 (unchanged), paper: true.
