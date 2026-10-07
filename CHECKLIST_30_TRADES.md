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
  active from the first restart with this code (restart time: Oct 07 21:46).
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
- Oct 07 (PAPER only): leader profile mirror_Leader-A for Leader-A (config leader_profiles),
  active from the first restart with this code (restart time: ________).
  Mode mirror: copy his buys AND his sells pro rata (he sells X% of his holding -> we sell X% of ours) on Solana
  (fraction = sold / his pre-balance in his sell tx) and xchain Robinhood/BSC (Relay + DEX sells, as the default leaders).
  No TP, no trailing. Safety stop -60% on the cost of what we still hold (replaces the global -20% Solana stop and the
  xchain safety stop for his positions). Size $200 / his median $500 = 0.400x his USD, cap $500 per token incl. his
  adds (xchain; Solana = 1 position/token), skip < $25 (= his buys < $62: 110 of 740).
  His median: 740 buys Sep 9 - Oct 7 2026 = 437 Relay (all chains; median $500, p25/p75 $100/$850) + 303 Solana
  USDC buys (median $330) -> combined median $500. No backtest: there is none for mirrored exits (whatif_xchain
  "leader" style is the closest; it has no Solana partials).
  Positions carry timing.profile / size_factor / exits (+ realized / initial_amount / partial_sells after partial
  sells on Solana); his positions opened before this restart keep their old rules (default exits, his sells mirrored
  on xchain only).
  PRE-REGISTERED TEST: judge only on positions opened under mirror_Leader-A after the restart, at ~30 new tokens
  (Solana + xchain together, same bar as trail_Leader-B): pass = total PnL > 0 with the best 3 tokens removed.
  Secondary: median peak after our fill vs his sells (did he sell near the top?), entry gap and sell gap vs his price.
  Check: python -m scripts.leader_report --from-id <first id after restart> ; python -m scripts.xchain_report --all ;
         python why.py Leader-A
HYPOTHESIS 4 (paper, from ID 230; see REVIEW_2026-10-07_night.md): the paper bot's global exits TP +30% / stop -20%,
  judged without Leader-G, on later trades only (150-trade threshold; +7.1%/trade in the model on the first 38).
  Count only positions without timing.profile. Profile leaders (trail_Leader-B since Oct 7 21:46, mirror_Leader-A since
  restart ____) are evaluated separately. whatif_ticksv2.py drops them by default and lists them (id, leader, profile).
  Check: python whatif_ticksv2.py --from-id 230
HYPOTHESIS 5 (paper, from ID 230): the venue split of the same positions (pool / vault / Jupiter-priced classes in
  whatif_ticksv2.py VENUE SPLIT) - does the result hold on the Jupiter-priced trades the pool model can't see?
  Count only positions without timing.profile. Profile leaders (trail_Leader-B since Oct 7 21:46, mirror_Leader-A since
  restart ____) are evaluated separately.
  Check: python whatif_ticksv2.py --from-id 230   (venue split at the end; --no-exclude-profiles to include them)
