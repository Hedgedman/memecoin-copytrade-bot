"""Offline test of the live price correction: python -m tests.test_live_correction"""
import asyncio, os, time
from solders.keypair import Keypair
from solders.pubkey import Pubkey

from bot.chains.solana import SolanaAdapter, LAMPORTS
from bot.models import Position
from bot.pricing.pools import PUMPSWAP_PROGRAM
from tests.test_pricing import token_acct_bytes

L = LAMPORTS


async def main():
    os.environ["TEST_BOT_KEY"] = str(Keypair())          # throwaway key, never funded
    mint = str(Pubkey.new_unique())
    a = SolanaAdapter({"rpc_http": "http://x", "rpc_ws": "ws://x", "leaders": [],
                       "private_key_env": "TEST_BOT_KEY", "max_market_cap_usd": 0, "pumpswap_fee_bps": 100}, paper=False)
    a._sol_usd_val, a._sol_usd_ts = 150.0, time.time() + 1e9
    a._hints[mint] = ("PoolAcc", "TV", "SV")

    async def fake_rpc(method, params):
        tok = lambda amt: {"owner": "Tokenkeg", "data": {"parsed": {"info": {"tokenAmount": {"amount": str(amt)}}}}}
        return {"context": {"slot": 1}, "value": [
            {"owner": "Tokenkeg", "data": {"parsed": {"info": {"supply": str(10**15)}}}}, None,
            {"owner": PUMPSWAP_PROGRAM, "data": ["", "base64"]}, tok(200_000_000 * 10**6), tok(100 * L)]}
    a._rpc = fake_rpc
    ok, _ = await a.pre_trade_check(mint, 0.5)
    assert ok
    tokens = a._pools[mint].state.buy_out(int(0.5 * L), 100)
    true_factor = 1.218                                    # Jupiter sees a 21.8% higher price
    jup_calls = []

    async def fake_quote(in_m, out_m, amount, slippage_bps=None, priority=False):
        jup_calls.append(amount)
        st = a._pools[mint].state
        return {"outAmount": str(int(st.sell_out(amount, 100) * true_factor))}
    a._quote = fake_quote

    p = Position(1, "solana", mint, "L", tokens, 0.5, time.time(), "sig", False)
    v_before = await a.quote_exit(p)                       # no factor yet -> Jupiter, schedules correction
    assert len(jup_calls) == 1
    await asyncio.sleep(0)                                 # let the background correction run
    await asyncio.sleep(0)
    assert abs(a._corr[mint] - true_factor) < 1e-3, a._corr
    n = len(jup_calls)
    v_pool = await a.quote_exit(p)                         # now pool x factor, no Jupiter call
    assert len(jup_calls) == n and abs(v_pool / v_before - 1) < 1e-3, (v_pool, v_before)
    # price moves in the pool: valuation follows instantly, still no Jupiter call
    a._on_account("SV", token_acct_bytes(120 * L), 2, time.time())
    a._on_account("TV", token_acct_bytes(166_666_667 * 10**6), 2, time.time())
    v_up = await a.quote_exit(p)
    assert v_up > v_pool * 1.3 and len(jup_calls) == n
    # implausible factor -> dropped, falls back to Jupiter
    true_factor = 3.0
    await a._update_corr(mint)
    assert mint not in a._corr
    await a.quote_exit(p)
    assert len(jup_calls) == n + 2
    print("live price correction ok: Jupiter until calibrated, then pool x factor, sanity-checked")


if __name__ == "__main__":
    asyncio.run(main())
    print("ALL PASSED")
