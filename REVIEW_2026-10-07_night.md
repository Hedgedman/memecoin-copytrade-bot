# Review 2026-10-07 (night): paper data since the 00:00 restart

**Data.** Dev paper bot (`copybot-dev/copybot.db`), read-only. Window: from the restart at Oct 7 00:00:49 to 12:30
local, about **12.5 hours**. No code or config was changed for this review.

**Paper settings in this window** (these are NOT the live H3 settings):

| Setting | Paper bot | Live H3 |
|---|---|---|
| Buy size | 2 SOL | 0.5 SOL |
| Take-profit / stop | TP +30% / stop −10% | trailing 30% after +50%, stop −20% |
| Market-cap filter | none (max $2M) | $30k–$500k |
| `only_first_leader_buy` | off | on |
| Re-entry cooldown | off | 1 hour |

Cross-chain: all 36 leaders, Robinhood + BSC paper-traded. This was the first night with the Step 2 code.

## Bottom line

1. **Solana paper made +2.50 SOL on 55 trades (+2.3% per trade). This is noise.**
   - The 90% confidence interval for the mean is −2.7% to +7.3% per trade.
   - Without the best 3 trades the night is **+0.05 SOL**.
   - The median trade lost −10%.
2. **What-if, on the 46 trades with tick data:**
   - Fixed TP +30% with stop −20% is the best of the styles you asked about, and it survives removing its best trade (+4.91 SOL).
   - It is also the Hypothesis 4 suggested yesterday, and these trades are new data for it, so this is a fair first
     check. But 46 trades are far from the 150 we set as the threshold.
   - Trailing 30% after +50% depends on one token (−6.2 of +8.2 SOL).
   - Following the leader's exit loses money.
3. **The live filters did not separate winners from losers tonight.** Trades they would have skipped averaged +1.4%, kept
   trades +3.4%, and both groups have a median of about −10%.
4. **Cross-chain: too little to judge.**
   - Only 4 positions, none closed: overnight brought just 11 Robinhood/BSC Relay buys (19 on all chains) across all
     leaders.
   - Detection works: buys and sells are seen within about 1–2 s.
   - The price gap at the leader's size is small (median +1.3% at +16 s), except on memecoins, where it was +48% to +79%.
5. **Bugs and issues found:** see the end. None of them distorted the results above.

---

## 1. Solana paper

**Totals.** 55 closed trades plus 2 still open (#192, #226). Each trade risks 2 SOL; 110.5 SOL was deployed in total.

| | trades | wins | PnL SOL | avg % | median % |
|---|---|---|---|---|---|
| all closed | 55 | 20 | **+2.50** | +2.3 | −10.1 |
| without best 1 | 54 | 19 | +1.60 | | |
| without best 3 | 52 | 17 | +0.05 | | |

- Standard deviation per trade: 22.8%.
- Bootstrap P(mean > 0) = **0.77**, with a 90% CI of −2.7% … +7.3%. So this cannot be told apart from zero.
- Median hold: 35 s.

**Per exit reason**

| exit | n | wins | PnL SOL | avg % | worst | best |
|---|---|---|---|---|---|---|
| take_profit | 19 | 19 | +12.26 | +32.1 | +22.0 | +44.8 |
| stop_loss | 36 | 1 | −9.75 | −13.5 | −36.1 | +4.2 |

The stop is nominally −10% but fills at a **median −13.0%**. 13 of 36 stops filled worse than −15%. The worst fills are
on Jupiter-priced venues, which are polled rather than streamed: −19% to −36% (#168, #177, #194, #197, #212). This is
the same stop slippage as in yesterday's review.

**Per leader.** Every leader here is far below the 8–10 trades we said we need to judge one, so treat this as a list of
what happened, not as evidence.

| leader | n | wins | PnL SOL | avg % |
|---|---|---|---|---|
| Leader-U | 12 | 4 | +0.48 | +2.0 |
| Leader-G | 8 | 3 | +0.72 | +4.5 |
| Leader-AJ | 7 | 3 | +0.88 | +6.2 |
| Leader-Q | 7 | **0** | **−2.04** | −14.5 |
| Leader-E | 4 | 2 | +0.54 | +6.7 |
| Leader-F | 4 | 2 | +0.51 | +6.4 |
| Leader-H | 3 | 3 | +1.86 | +30.9 |
| Leader-AI, Leader-K, Leader-N | 2 each | 1 each | +0.63 / −0.31 / +0.21 | |
| Leader-V, Leader-J, Leader-S, Leader-P | 1 each | 0 | −0.40 / −0.05 / −0.28 / −0.26 | |

`Leader-Q`, called "most consistent" in CLAUDE.md, went 0 for 7. That's worth watching, but at 7 trades it is still noise.

**Per market cap at fill** (bot-scale market cap)

| bucket | n | wins | PnL SOL | avg % |
|---|---|---|---|---|
| < $30k | 3 | 2 | +1.06 | +17.6 |
| $30k–100k | 16 | 7 | +1.70 | +5.3 |
| $100k–500k | 19 | 5 | −0.95 | −2.5 |
| > $500k | 17 | 6 | +0.69 | +2.0 |

No bucket has enough trades to conclude anything. The < $30k row is 3 trades. It does not overturn the earlier
finding that micro caps lose; it just doesn't add to it.

## 2. What-if on the same Solana trades

**Method.** `whatif_ticksv2.py` logic (pool ticks, 0.8 s exit delay, 0.01 SOL fee per sell) on the **46 of 55** trades
that have tick data.

- The 9 trades without ticks (Jupiter-priced venues) lost **−1.85 SOL** in reality and cannot be replayed. The what-if
  therefore looks better than the night as a whole.
- On these 46 trades the bot actually made **+4.35 SOL**, while the model of the same exits says **+5.37 SOL**. So the
  model is **about 1 SOL (≈2 points per trade) too optimistic**, mostly from stop fills. Subtract roughly that from
  every row.
- **Moonbag:** not built, so not tested.

| style | total SOL | wins | censored | avg % | P(mean>0) | without its best trade |
|---|---|---|---|---|---|---|
| actual bot (TP +30% / SL −10%) | +4.35 | 18/46 | 0 | +4.7 | 0.93 | +3.45 |
| A: model of current exits (TP +30% / SL −10%) | +5.37 | 20/46 | 0 | +5.8 | 0.96 | +3.98 |
| B: trailing 30% after +50%, SL −20% (live H3) | +8.16 | 20/46 | 2 | +8.8 | 0.87 | **+1.94** (#182 alone +6.22) |
| C: leader exit, safety SL −40% | **−5.30** | 18/46 | 9 | −5.7 | 0.16 | −7.83 |
| C′: leader exit + trailing 30%/+50%, SL −40% | +1.21 | 24/46 | 4 | +1.3 | 0.58 | −1.32 |
| D: fixed TP +30% / SL −20% | +6.30 | 23/46 | 0 | +6.8 | 0.94 | **+4.91** |
| D without Leader-G (= H4 as proposed yesterday) | +5.45 (38 trades) | | 0 | +7.1 | 0.93 | +4.07 |

**How to read this**

- **B (the live H3 exits) is a one-runner result.** Remove token #182 (3cpBM7) and it drops from best to worst of the
  positive styles. This is the same pattern as the first trailing hypothesis, which failed out of sample.
- **C (follow the leader's exit) is clearly bad here.** Leaders sold a median 1.8 min after our entry, and 9 of 46 trades
  never saw a leader sell within the data. This answers the "position 39" idea in CLAUDE.md for now: on this sample,
  following his exit with a −40% safety stop loses about 6% per trade.
- **D / H4 (TP +30%, stop −20%) is the only style that stays clearly positive without its best trade.** These trades
  came after yesterday's review, so this is new data for it, not a fitted result. But:
  - it is 46 trades, against the 150 we set beforehand;
  - after the ~2-point model optimism, a realistic estimate is about +4–5% per trade;
  - a −20% stop will slip more on Jupiter venues, which are the trades the model can't see.

  So it is encouraging, and not a reason to change anything yet.
- The full `whatif_ticksv2.py` grid tested 77 settings. Its overall best (TP +50% / SL −20%, +8.73) is partly luck by
  construction, so ignore it.

## 3. Cross-chain paper (Robinhood + BSC; Ethereum/Base watch-only)

### Overnight activity
- **Relay trades recorded since 00:00:** 19 buys and 32 sells, plus 23 bridges.
  - Buys: 7 Robinhood, 3 BSC and 6 Ethereum seen by the stream; 1 Robinhood, 1 Ethereum and 1 Base found about 50 min
    late by the startup poll.
  - Bridges include 7 to an unmapped chain id, **1337** (most likely Hyperliquid; to be confirmed via Relay `/chains`).
- **Paper positions opened: 3** (#184, #221, #224), plus #130 still open from yesterday.
- **Skipped buys: 7.**
  - 3: the $2,000 cap on V4 was already reached (Leader-K bought $15k + $10k).
  - 3: the leader's buy was too small for us (our 0.4x size under $50).
  - 1: stale (found by poll 53 min late).

Overnight is clearly quiet. Step 1's estimate of about 68 qualifying buys a day either overstates night activity or
mostly happens in US hours. Judge the volume again after a full day.

**How fast we see his trades** (`detect_delay_s`):

| side / chain | n | detection delay |
|---|---|---|
| buy, all chains (stream) | 16 | about 0 s (−2 to +1 s) |
| sell, BSC / Robinhood (payout stream) | 19 | about 0–2 s |
| sell, Base | 5 | 5 s |
| sell, Ethereum | 7 | 24–39 s |

- Negative values come from Relay's whole-second timestamps, so read them as "immediate".
- Ethereum sells are slow because Relay only pays out after Ethereum confirms.
- **The payout stream works:** every BSC and Robinhood sell was pushed, none needed polling.

### Price ladder
Relay `/price` at the **leader's size**, 3/6/10/16 s after we detected his trade. A positive gap on a buy means we pay
more than he did; a negative gap on a sell means we get less.

| side | +3 s | +6 s | +10 s | +16 s | n |
|---|---|---|---|---|---|
| buy, median gap | +2.7% | +1.6% | +1.4% | +1.3% | 14–15 |
| sell, median gap | −0.9% | −1.2% | −0.7% | −1.9% | 29–30 |

- **The medians hide two different kinds of trade:**
  - Tokenized stocks and gold (XAUt, NVDA, QQQ, ZEC, SKHY, mostly Leader-M and Leader-AE): gap 0 ± 0.5%. Nothing to gain
    or lose.
  - Memecoins: **EPOCH (Leader-AJ) +63% → +59%** and **BCI (Leader-Q) +61% → +48%** at every step. On these the price
    had already moved far past his fill before anyone could copy it, and waiting 16 s did not bring it back.
- Large Ethereum and Base sells by Leader-O ($35k–$72k) show −3% to −10% at his size. Our size is 0.4x, so expect less
  slippage on ours.
- No step of the ladder is consistently better than another. At n = 14 buys, the 3 s vs 16 s difference is noise.

### Positions

| pos | leader | chain | token | cost $ | entry gap (our fill vs his price) | status | value now* |
|---|---|---|---|---|---|---|---|
| 130 | Leader-A | robinhood | BORRO | 440 | +70.3% | open (since yesterday) | 344 (−22%) |
| 184 | Leader-K | robinhood | V4 | 2,000 | −0.2% | open | 2,190 (+10%) |
| 221 | Leader-Q | robinhood | BCI | 480 | +41.7% | open | 629 (+31%) |
| 224 | Leader-G | bsc | 拍手黑猫 | 1,400 | +12.8% / +5.5% (add-on) | open, 2 partial sells | 2,266 (+62%) |

\* Value now = realized sells + Relay quote for the remaining tokens (latest mark).

- **Closed results with mirrored exits: none yet (0 closed).**
- The only mirrored exits so far are #224's two partial sells. We sold 31% and then 25%, 17 s after him, at −14.7% and
  −3.0% vs his sell price.
- The +62% / +31% / +10% marks are 3 open trades. **Do not read them as a result.**

## 4. Would the live filters have skipped these trades?

The live filters (market cap $30k–$500k, `only_first_leader_buy`, 1 h re-entry cooldown) applied to the 55 closed paper
trades. The skipped trades' results are what paper actually got on them.

| group | n | wins | PnL SOL | avg % | median % |
|---|---|---|---|---|---|
| skipped: market cap outside $30k–$500k | 20 | 8 | +1.75 | +4.4 | −10.3 |
| skipped: leader already held the token (add-on) | 11 | 3 | −1.29 | −5.8 | −13.7 |
| skipped: re-entry within 1 h | 17 | 5 | −1.00 | −2.9 | −10.6 |
| **skipped by any filter** | **32** | 12 | +0.92 | +1.4 | −10.5 |
| **kept by all filters** | **23** | 8 | +1.58 | +3.4 | −7.9 |

Kept trades without their best one: +0.68 SOL.

- The filters would have removed 58% of the trades. The kept group did slightly better per trade (+3.4% vs +1.4%), but
  this is noise: the spread per trade is about 23%, so the gap is a fraction of one standard error.
- **Add-on buys (−5.8%) and re-entries (−2.9%)** lost a little in this sample, consistent with the live hint from
  before. Paper contradicted that earlier, so this still counts as unresolved, not as confirmation.
- **Outside the market-cap range was positive (+4.4%)**: the > $500k trades did fine (17 trades, +0.69 SOL), and 3
  micro caps happened to win.
- The cooldown check uses the paper bot's own history. A live bot with the filter on would have had different earlier
  trades, so this is an approximation.

## What is noise and what is not

**Noise, don't act on it:**
- Tonight's Solana profit.
- Every per-leader and per-market-cap row.
- The filter comparison.
- All cross-chain PnL.
- The difference between ladder steps.

**Signal, consistent with earlier data:**
- Stops fill about 3 points worse than nominal on pool venues and 9–26 points worse on Jupiter venues.
- Trailing results hinge on single runners.
- Following the leader's exit loses on Solana.
- Cross-chain memecoin entries can be 40–70% above the leader's price even with about 0 s detection. Tokenized
  stocks and gold copy at about 0% gap.

**Worth tracking toward the 150-trade threshold:** H4 (TP +30% / stop −20%, without Leader-G) is +7.1%/trade
in the model on 38 new trades, and stays positive without its best trade.

## Issues found (not fixed; this review was read-only)

1. **BSC `eth_getLogs` error:** "block range extends beyond current head block". The public BSC RPC is load-balanced,
   and the log query hit a node one block behind the one that gave the block number. The next round retried, so no
   sells were missed, but the bot logged "DEX sells NOT detected". Fix: query up to `latest − 2` and only warn after
   repeated failures. Setting `BSC_RPC_URL` (Alchemy) also gives a fallback.
2. **Unmapped Relay chain 1337** (7 bridges): add a name once Relay `/chains` confirms what it is.
3. **One Relay request was never resolved** (`xchain_unresolved`, after 180 s), plus 2 `xchain_evm_tx` events on BSC
   (nonce +1 and +4 without a Relay request, possibly EVM DEX trades). Both need a look.
4. **9 Solana trades (16%) have no tick data**, because they are on Jupiter-priced venues. They lost −1.85 SOL, and the
   what-if can't see them. This makes every what-if look about 1 SOL better than the night was.
