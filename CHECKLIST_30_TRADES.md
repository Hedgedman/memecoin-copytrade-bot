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
  active from the first restart with this code (restart time: 2026-10-08 15:43).
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
  restart 2026-10-08 15:43) are evaluated separately. whatif_ticksv2.py drops them by default and lists them (id, leader, profile).
  Check: python whatif_ticksv2.py --from-id 230
HYPOTHESIS 5 (paper, from ID 230): the venue split of the same positions (pool / vault / Jupiter-priced classes in
  whatif_ticksv2.py VENUE SPLIT) - does the result hold on the Jupiter-priced trades the pool model can't see?
  Count only positions without timing.profile. Profile leaders (trail_Leader-B since Oct 7 21:46, mirror_Leader-A since
  restart 2026-10-08 15:43) are evaluated separately.
  Check: python whatif_ticksv2.py --from-id 230   (venue split at the end; --no-exclude-profiles to include them)

---- Oct 8 2026 (night build, see BUILD_NOTES_2026-10-08.md; restart pending) ----
DATA RULE: positions with a single-step mark change > 50% are excluded from ALL tests unless the change is
  confirmed by an independent source (a Jupiter-vs-pool calibration within 10 min, or an add-on fill).
  Applies to winners and losers alike. Currently: 299, 192, 269, 40 (python -m scripts.flag_positions).
  whatif_ticksv2.py, scripts/leader_report.py, scripts/leader_selection.py and scripts/missed_runners.py drop
  them by default and list them; --include-flagged shows them. The bot flags live source switches itself
  (timing.flagged) - those count too.
HYPOTHESIS 6 (paper, EVM normal leaders): Robinhood/BSC positions of NON-profile leaders use the global exits:
  TP +30% on cost, stop -20% from the first Relay quote after the fill, max hold 48 h, his sells IGNORED
  (timing.exits.rule = "H6"). Starts at restart 2026-10-08 15:43 (from id 366), commit 8bba0c9.
  Count: EVM positions opened after that WITHOUT timing.profile (= with timing.exits.rule "H6"):
    sqlite3 copybot.db "SELECT COUNT(*), SUM(status='closed'), ROUND(SUM(CASE WHEN status='closed' THEN exit_value-cost END),0)
      FROM positions WHERE chain!='solana' AND timing LIKE '%\"rule\": \"H6\"%';"
    python whatif_xchain.py --profile H6 --list ; python -m scripts.xchain_report --all (rows labelled H6)
  Interim at 25 closed: PnL w/o best < 0 -> stop. Pass at 50 closed: PnL > 0 w/o best.
  Positions opened before the restart (130, 184, 229, 257 open tonight) keep the legacy rules (his sells
  mirrored, safety stop -60%) and are NOT part of H6.
mirror_ladder_Leader-B (paper, runner test) for Leader-B: his buys AND adds
  copied at 0.219x (cap $1,000/token incl. adds, min $25), his sells mirrored pro rata, PLUS own ladder: sell
  25% of the current holding at 10x / 25x / 50x / 100x our average entry (each once; adds raise the average),
  safety stop -60% on the cost of what we still hold, no trailing, no TP. Own limits: 10 open tokens, $500/day.
  Starts at restart 2026-10-08 15:43 (from id 366). Kill if after 100 positions the average loss on positions WITHOUT a 10x is
  worse than -40%, or after 200 positions no token reached 10x. Review at 6 months. NOT judged on "w/o best 3".
  Check: python whatif_ticksv2.py --profile mirror_ladder_Leader-B ; python whatif_xchain.py --profile mirror_ladder_Leader-B --list
         sqlite3 copybot.db "SELECT id, chain, status, ROUND(cost,2), ROUND(exit_value,2), exit_reason, json_extract(timing,'$.ladder_fired')
           FROM positions WHERE timing LIKE '%mirror_ladder_Leader-B%' ORDER BY id;"
mirror_Leader-A (paper) for Leader-A: enabled at restart 2026-10-08 15:43 (from id 366) with
  mode mirror, his buys AND adds copied at 0.400x (cap $1,000/token incl. adds, min $25), his sells mirrored
  pro rata, stop -60% on the rest, marks every 30 s, 10 open tokens, $500/day. (Fill the "restart ____" blanks
  of the Oct 7 mirror_Leader-A entry above with the same time.) Pre-registered test unchanged: ~30 new tokens,
  pass = PnL > 0 without the best 3.
LEADER SELECTION rules (scripts/leader_selection.py, Solana + xchain combined, flagged positions excluded,
  profile positions not counted):
  CUT     = median entry gap > +20% over >= 5 entries, OR (>= 15 closed AND PnL w/o best < 0 AND avg < -5%),
            OR no buy signal in 14 days
  PROMOTE = >= 30 closed AND PnL w/o best 2 > 0 AND median gap < +5%
  WATCH   = everything else. The script prints which rule fired.
  Leader status (config chains.solana.leader_status): active = paper trades; watch = signals, entry gaps and
  post-signal price paths recorded, NO positions on any chain, never blocks a token.
  Leader-C = watch since restart 2026-10-08 15:43 (gap +30% over 7 entries).
  First run Oct 8 (whole history, SOL $200): CUT Leader-Q (gap +48%/27), Leader-D (20 closed, w/o best -$672,
  avg -6.5%), Leader-O (gap +39%), Leader-P (gap +20%); 0 PROMOTE. Not acted on (judge at 8-10+ trades).
trail_Leader-B: closed Oct 8 2026, replaced by mirror_ladder_Leader-B (0 positions were opened under trail_Leader-B).
Profiles since Oct 8: profile positions are keyed by (leader, token), separate from normal positions, outside
  max_open_positions / daily_loss_limit / xchain 50-open / 5-per-leader; the min_leader_buy_sol filter does not
  apply to profile leaders (their min_usd does). H4/H5/H6 counts exclude them (timing.profile).
Tick retention since Oct 8: ticks_after_close_h 24 (was 6 h); ticks older than 7 days -> 1-minute bars with
  python -m scripts.compact_ticks (bot STOPPED). whatif_ticksv2 replays old positions on the bars (minute
  precision) and requires the tick series to start within 30 s of our fill (258 and 268 are "not replayable").

SCANNER CALIBRATION (registered 2026-10-09, before results): the wallet scanner
becomes the screening gate for new leaders only if the copy-sim (check 12) has
Spearman rho >= 0.54 vs our paper PnL over the 14 calibration leaders AND it
marks Leader-E PAPER CANDIDATE while rejecting Leader-D and Leader-AI.
Otherwise it stays informational and new leaders are judged in paper only.
