# copybot

Copy-trading bot: watches leader wallets, buys behind them, exits on TP / SL / max hold / leader exit.
Chain-agnostic core; adapters for Solana, BSC and Robinhood Chain.

## Setup
```
pip install -r requirements.txt
cp config.example.yaml config.yaml          # fill in RPC + leaders
export SOL_PRIVATE_KEY=... BSC_PRIVATE_KEY=... ROBINHOOD_PRIVATE_KEY=...  # live only; DEDICATED wallets
python -m tests.test_core && python -m tests.test_evm   # offline tests
python -m bot.main -c config.yaml
```
`touch STOP` blocks new buys immediately (open positions still get managed).

## Rollout (do not skip)
1. **Paper mode** (`paper: true`), at least a few days. Uses real Jupiter quotes, sends nothing.
   Check `copybot.db`: signal latency, skip reasons, and the `marks` table (price path per position).
2. **Live, tiny size** (`buy_amount` ~0.1 SOL). Compare real fills with paper fills: the gap is your true cost.
3. Scale only if live results at small size match paper.

## How it works
- **Detection**: `logsSubscribe` per leader -> `getTransaction` -> token-balance deltas of the leader.
  DEX-agnostic (pump.fun, Raydium, Meteora, DFlow-routed fills all look the same).
  Only SOL/WSOL-quoted single-token swaps are copied.
- **Pre-trade checks**: freeze/mint authority on the token, and a buy->sell round-trip quote
  (catches honeypots and heavy taxes).
- **Execution**: Jupiter quote + swap, priority fee capped by `max_priority_fee_lamports`.
- **Exits**: marked every `poll_interval_s` on the quote for selling the WHOLE position,
  so slippage is inside the PnL. Failed sells retry with escalating slippage (30% -> 50% -> 90%).

## Known limits / things to watch
- **The 4% stop is a trigger, not a guarantee.** Tokens gap; tests show a stop triggered at -30%.
  Watch your *average realised loss*, not the setting.
- **Latency**: `confirmed` commitment + getTransaction + 2 Jupiter calls ~ 1-3 s per entry.
  The `max_signal_age_s` filter and latency logs tell you if this kills the edge.
  Upgrades in order of impact: Yellowstone gRPC detection, skip the pre-trade round-trip quote
  for known-safe launchpads, direct instruction building, Jito bundles.
- **"CHECK WALLET" errors**: a tx that didn't confirm in time may still land. Reconcile manually.
- Positions survive restarts (SQLite), but tokens bought outside the bot are ignored.
- Verify the current Jupiter API endpoint/limits before going live; the free tier is rate-limited.

## Pool pricing, timing and tick data (v2)
- **Pool pricing** (`bot/pricing/pools.py`): Pump.fun curve and PumpSwap pools are streamed over ONE
  websocket (`accountSubscribe`). Safety checks, market cap, exit marks, stops and paper fills are computed
  locally: 1 RPC call per entry check instead of 2-4 Jupiter calls, and stops react to every trade in the pool
  instead of every 3-20 s. Other venues and graduated curve tokens fall back to Jupiter.
- **Paper fills are honest now**: paper waits `paper_land_delay_s` before filling (others buy first), and
  exits are valued on a "shadow pool" that includes your own buy. Old paper data sold into a pool without
  your buy, which booked your price impact as a loss twice.
- **Jupiter priority lane**: entry checks, buys and sells never queue behind background quotes.
- **Timing**: every position stores stage timestamps and slots (`positions.timing`, JSON). `python timings.py`.
- **Ticks**: every pool update for open positions, recently closed ones and skipped signals goes to the
  `ticks` table. `python whatif_ticks.py` replays them with exact pool math and a landing delay, and shows
  what speed is worth.
- **Calibrate fees** before trusting any of it: `python calibrate.py <leader_buy_sig> <leader_wallet>`.
- Tests: `python -m tests.test_pricing`.

## EVM chains (BSC, Robinhood Chain)
- **Detection**: polls `eth_getLogs` for ERC-20 Transfers into/out of leaders, keeps only txs SENT BY
  the leader (drops airdrops and address-poisoning dust). Bonding-curve buys are detected too.
- **Execution**: Uniswap-V2-style router only (PancakeSwap V2 / Uniswap V2). Tokens still on a
  bonding curve (four.meme, Robinhood launchpads) or only on V3/V4 are skipped and logged as
  `no V2 route`. Run paper mode, then count those skips per venue to decide what to add next.
- **Transfer taxes** are not in the pre-trade quote. The real received amount is read from the
  receipt, so PnL is correct, but a high-tax token can still make sells revert.
- **BSC**: higher gas price can help ordering (`gas_price_multiplier`); a private/builder RPC
  (`send_rpc`) reduces exposure to sandwich bots.
- **Robinhood Chain**: ~100 ms blocks, first-come-first-served sequencer. Paying more gas does
  NOT get you in earlier; only latency does. `router_v2` must be filled in and verified by you.
  The public RPC is rate-limited and not meant for production.
- Each chain uses its own wallet/key env var; keep them separate.

## Adding a chain or venue
Implement `ChainAdapter` (bot/chains/base.py) and register it in `make_adapter` in bot/main.py.
