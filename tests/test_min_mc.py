"""Minimum market cap filter: python -m tests.test_min_mc"""
import asyncio, base64, struct, time
from solders.pubkey import Pubkey
from bot.chains.solana import SolanaAdapter, LAMPORTS
from bot.pricing.pools import PUMP_PROGRAM

L = LAMPORTS


def curve(vtok, vsol):
    return b"\x00" * 8 + struct.pack("<QQQQQ?", vtok, vsol, vtok // 2, vsol // 2,
                                     10**15, False) + b"\x01" * 32


async def check(vsol, **cfg):
    mint = str(Pubkey.new_unique())
    a = SolanaAdapter({"rpc_http": "http://x", "rpc_ws": "ws://x", "leaders": [], **cfg}, paper=True)
    a._sol_usd_val, a._sol_usd_ts = 150.0, time.time() + 1e9
    k = 30 * L * 1_073_000_000 * 10**6
    vtok = k // vsol
    async def fake_rpc(method, params):
        return {"context": {"slot": 1}, "value": [
            {"owner": "Tokenkeg", "data": {"parsed": {"info": {"supply": str(10**15)}}}},
            {"owner": PUMP_PROGRAM, "data": [base64.b64encode(curve(vtok, vsol)).decode(), "base64"]}]}
    a._rpc = fake_rpc
    return await a.pre_trade_check(mint, 0.5)


async def main():
    # fresh curve: ~28 SOL mcap * $150 = ~$4.2k; vsol 120 SOL -> mcap ~ $67k
    ok, why = await check(30 * L, min_market_cap_usd=30000, max_round_trip_loss=1)
    assert not ok and "< min" in why, why
    ok, why = await check(120 * L, min_market_cap_usd=30000, max_market_cap_usd=500000, max_roundtrip_loss=0.5)
    assert ok and "mc $" in why, why
    ok, why = await check(120 * L, min_market_cap_usd=0, max_market_cap_usd=50000, max_roundtrip_loss=0.5)
    assert not ok and "> $" in why, why
    print("min/max market cap filter ok:", why)


if __name__ == "__main__":
    asyncio.run(main())
    print("ALL PASSED")
