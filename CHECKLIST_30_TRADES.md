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
- Oct 07 (PAPER only): leader profile trail_Leader-B for Leader-B (config leader_profiles),
  active from the first restart with this code (note the restart time here: ________).
  Solana + xchain: no TP, trailing 35% once 1.5x, stop -60% before that; his sells NOT followed.
  Size $200 / his median $913 = 0.219x his USD, cap $500 per token incl. his adds (xchain; Solana = 1 position/token),
  skip < $25. Cap $500, not $1,000: in the backtest (exact_bot.py, merged adds as the bot does) $1,000 added only
  +$179 base / +$39 stress, did worse without the best 3, and raised peak capital $2.0k -> $2.8k.
  Backtest caveats: IN-SAMPLE (1.5x/35%/-60% picked on the same data), EXCLUDES ethereum/arc buys (bot can't trade them),
  28 tokens; $500 cap: base +$1,782 (+20%), stress (exits at candle low) +$795, and without the best 3 tokens
  only +$506 base / -$54 stress = close to zero. Two Solana runners carry most of the profit. Positions carry timing.profile / size_factor / exits; older positions of his keep the default exits.
  PRE-REGISTERED TEST: judge only on positions opened under trail_Leader-B after the restart, at ~30 tokens:
  pass = total PnL > 0 with the best 3 tokens removed. Backtest (Sep 16-Oct 7, 55 buys) is in-sample: don't count it.
  Check: python whatif_xchain.py --profile trail_Leader-B --list ; python whatif_ticksv2.py --profile trail_Leader-B ;
         python -m scripts.xchain_report --all
