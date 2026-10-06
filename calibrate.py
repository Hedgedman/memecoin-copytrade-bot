"""Calibrate pool fee settings against Jupiter, and check we model the same pool Jupiter uses.

Usage (from the copybot folder):
    python calibrate.py <leader_tx_signature> <leader_wallet>

For each sample it:
  1. reads the pool the leader traded with (the same way the bot does),
  2. asks Jupiter for a 0.5 SOL buy quote and a sell quote for those tokens,
  3. solves which fee makes our pool math match Jupiter (buy side and sell side),
  4. shows which pool Jupiter routed through, so you can see if it's the same one.

Read the result:
  same pool + fees ~equal on buy and sell  -> that's the real fee: put it in config.yaml
  DIFFERENT pool                           -> not a fee measurement; Jupiter used another pool
  same pool but a huge fee (> 300 bps)     -> our model of this pool is wrong: send me the output
"""
import asyncio
import base64
import statistics
import sys

import yaml

from bot.chains.base import RateLimited
from bot.chains.solana import SolanaAdapter, WSOL, LAMPORTS, parse_wallet_swap, parse_stable_swap, venues_in_tx
from bot.pricing.pools import find_pool_vaults, curve_pda


def solve(fn, target, lo=0.0, hi=5000.0):
    """Fee in bps such that fn(fee) ~ target, fn decreasing in fee."""
    for _ in range(50):
        mid = (lo + hi) / 2
        if fn(int(round(mid))) > target:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


async def quote(a, *args):
    for i in range(4):
        try:
            return await a._quote(*args, priority=True)
        except RateLimited:
            await asyncio.sleep(3 * (i + 1))   # the running bot shares the same Jupiter limit
    raise RuntimeError("Jupiter keeps rate-limiting; try again in a minute")


async def check_pool_layout(a, pool_addr, mint, vaults):
    """Decode the PumpSwap pool account and check it points at the vaults we read.
    Assumed layout after the 8-byte discriminator: bump u8, index u16, creator, base_mint,
    quote_mint, lp_mint, pool_base_token_account, pool_quote_token_account (32 bytes each)."""
    from solders.pubkey import Pubkey
    info = await a._rpc("getAccountInfo", [pool_addr, {"encoding": "base64"}])
    if not info or not info.get("value"):
        print("  pool account not found"); return
    d = base64.b64decode(info["value"]["data"][0])
    pk = lambda off: str(Pubkey.from_bytes(d[off:off + 32])) if len(d) >= off + 32 else "?"
    base_mint, quote_mint, base_ta, quote_ta = pk(43), pk(75), pk(139), pk(171)
    layout_ok = mint in (base_mint, quote_mint)
    print(f"  pool account: {len(d)} bytes | layout guess {'OK' if layout_ok else 'NOT confirmed'}")
    if layout_ok:
        print(f"  base mint  {base_mint[:8]}  vault {base_ta[:8]}")
        print(f"  quote mint {quote_mint[:8]}  vault {quote_ta[:8]}")
        ours = {vaults[1], vaults[2]}
        print(f"  our vaults {vaults[1][:8]} (token), {vaults[2][:8]} (SOL) -> "
              f"{'MATCH the pool' if ours == {base_ta, quote_ta} else 'DO NOT match the pool!'}")


def route_str(q):
    return " -> ".join(f"{r['swapInfo'].get('label', '?')} {r['swapInfo'].get('ammKey', '?')[:8]}"
                       for r in q.get("routePlan", []))


async def main(sig, wallet):
    cfg = yaml.safe_load(open("config.yaml"))["chains"]["solana"]
    a = SolanaAdapter(cfg, paper=True)
    await a.start()
    try:
        tx = await a._get_tx(sig)
        if not tx:
            print("Transaction not found. Check the signature."); return
        parsed = parse_wallet_swap(tx, wallet) or parse_stable_swap(tx, wallet)   # SOL- or USDC-paid (FOMO)
        if not parsed or parsed[0] != "buy":
            print("Not a buy by that wallet. Pick another leader buy."); return
        mint, venues = parsed[1], venues_in_tx(tx)
        v = find_pool_vaults(tx, mint, wallet)
        if v:
            a._hints[mint] = v
        print(f"token {mint}\nvenues {venues}")
        _, pool = await a._resolve(mint)
        if not pool:
            print("Not on the pump.fun curve and no PumpSwap pool found -> nothing to calibrate."); return
        our_pool = v[0] if pool.venue == "pumpswap_amm" else curve_pda(mint)
        print(f"our pool: {pool.venue} {our_pool[:8]}  (config fee {a.fee_bps[pool.venue]} bps)")
        if pool.venue == "pumpswap_amm":
            await check_pool_layout(a, our_pool, mint, v)
        print()
        fee_cfg = a.fee_bps[pool.venue]
        ratios = []
        lamports = int(0.5 * LAMPORTS)
        buys, sells = [], []
        for i in range(5):
            _, pool = await a._resolve(mint)
            st = pool.state
            qb = await quote(a, WSOL, mint, lamports)
            out = int(qb["outAmount"])
            qs = await quote(a, mint, WSOL, out)
            back = int(qs["outAmount"])
            same = all(r["swapInfo"].get("ammKey") == our_pool for r in qb.get("routePlan", []))
            fb = solve(lambda f: st.buy_out(lamports, f), out)
            fs = solve(lambda f: st.sell_out(out, f), back)
            tag = "same pool" if same else "DIFFERENT pool"
            # with the configured fee: Jupiter / our model, buy side and sell side
            rb = out / max(1, st.buy_out(lamports, fee_cfg))
            rs = back / max(1, st.sell_out(out, fee_cfg))
            print(f"  sample {i+1}: buy fee {fb:.0f} bps, sell fee {fs:.0f} bps | "
                  f"Jupiter/ours buy {rb:.3f} sell {rs:.3f} (product {rb*rs:.3f}) | {route_str(qb)} [{tag}]")
            if same:
                buys.append(fb); sells.append(fs); ratios.append((rb, rs))
            await asyncio.sleep(1.5)
        if ratios:
            rb = statistics.median(r[0] for r in ratios); rs = statistics.median(r[1] for r in ratios)
            print(f"\nJupiter/ours at {fee_cfg} bps: buy {rb:.3f}, sell {rs:.3f}, product {rb*rs:.3f}")
            print("  product ~1.00 -> only the price LEVEL differs; PnL % in paper is barely affected")
            print("  product < 0.98 -> real round trips are worse than our model: paper is too optimistic")
        if buys:
            print(f"\nsame-pool samples: buy median {statistics.median(buys):.0f} bps, "
                  f"sell median {statistics.median(sells):.0f} bps")
            m = statistics.median(buys + sells)
            if m > 300:
                print("That is far above any normal pool fee: our model of this pool is off. Send me this output.")
            else:
                key = 'pumpfun_fee_bps' if pool.venue == 'pumpfun_curve' else 'pumpswap_fee_bps'
                print(f"-> {key} ~ {m:.0f}. Compare with other tokens before changing config.yaml.")
        else:
            print("\nJupiter never used our pool: no fee measurement possible for this token. Try another one.")
    finally:
        await a.close()


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(__doc__); sys.exit(1)
    asyncio.run(main(sys.argv[1], sys.argv[2]))
