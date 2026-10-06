"""Meteora DAMM v2 (vault-priced) tests: python -m tests.test_vault"""
import asyncio, tempfile, time
from solders.pubkey import Pubkey

from bot.chains.solana import SolanaAdapter, LAMPORTS, WSOL
from bot.models import Position
from bot.store import Store
from tests.test_pricing import token_acct_bytes

L = LAMPORTS


def adapter(true_factor):
    mint = str(Pubkey.new_unique())
    a = SolanaAdapter({"rpc_http": "http://x", "rpc_ws": "ws://x", "leaders": [], "paper_land_delay_s": 0,
                       "max_market_cap_usd": 0, "buy_amount": 0.5, "paper_fee_per_tx_sol": 0.01}, paper=True)
    a._sol_usd_val, a._sol_usd_ts = 150.0, time.time() + 1e9
    a._hints[mint] = ("PoolAuthority", "TV", "SV")
    a._hint_venue[mint] = "meteora_damm_v2"

    async def fake_rpc(method, params):
        tok = lambda amt: {"owner": "Tokenkeg", "data": {"parsed": {"info": {"tokenAmount": {"amount": str(amt)}}}}}
        return {"context": {"slot": 1}, "value": [
            {"owner": "Tokenkeg", "data": {"parsed": {"info": {"supply": str(10**15)}}}}, None,
            {"owner": "11111111111111111111111111111111", "lamports": 0, "data": ["", "base64"]},
            tok(300_000_000 * 10**6), tok(60 * L)]}
    a._rpc = fake_rpc
    calls = []

    async def fake_quote(in_m, out_m, amount, slippage_bps=None, priority=False):
        calls.append((in_m, amount))
        pool = a._pools.get(mint)
        st = pool.state if pool else None
        if in_m == WSOL:   # buy
            return {"outAmount": str(int(st.buy_out(amount, 100) / true_factor[0])) if st else "1000"}
        return {"outAmount": str(int(st.sell_out(amount, 100) * true_factor[0])) if st else str(amount // 10**6)}
    a._quote = fake_quote
    factors = []
    a.set_factor_sink(lambda tok, ts, f: factors.append(f))
    return a, mint, calls, factors


async def main():
    tf = [1.10]
    a, mint, calls, factors = adapter(tf)
    ok, why = await a.pre_trade_check(mint, 0.5)
    assert ok and "meteora_damm_v2" in why, why                    # calibrated, then checked on the pool
    assert abs(a._corr[mint] - 1.10) < 1e-3 and factors, (a._corr, factors)
    fill = await a.buy(mint, 0.5)                                  # paper fill on vault venue -> Jupiter
    assert fill.ok and fill.model.startswith("jupiter"), fill.model
    p = Position(1, "solana", mint, "L", fill.token_amount, fill.quote_amount, time.time(), None, True)
    n = len(calls)
    v0 = await a.quote_exit(p)                                     # pool x factor, no Jupiter call
    assert len(calls) == n and 0.9 < v0 / 0.5 < 1.05, v0
    a._on_account("SV", token_acct_bytes(80 * L), 2, time.time())
    a._on_account("TV", token_acct_bytes(225_000_000 * 10**6), 2, time.time())
    v1 = await a.quote_exit(p)
    assert v1 > v0 * 1.4 and a._pools[mint].state.venue == "meteora_damm_v2", (v0, v1)
    a._flush_ticks()

    tf2 = [3.0]                                                    # wrong pool -> rejected, Jupiter only
    b, mint2, calls2, _ = adapter(tf2)
    ok, why = await b.pre_trade_check(mint2, 0.5)
    assert mint2 in b._bad_vault and mint2 not in b._pools and "jupiter" in why, why
    print("damm v2: calibrated before use, paper fill via Jupiter, marks = pool x factor, bad pool rejected")

    # what-if values vault ticks with the stored factors
    from whatif_ticksv2 import load
    store = Store(tempfile.mktemp())
    pos = Position(None, "solana", "M", "L", 10**12, 0.51, 1000.0, None, True)
    pos.id = store.open_position(pos); pos.status = "closed"; pos.exit_value = 0.5; pos.closed_at = 1100
    store.close_position(pos)
    store.record_ticks([(1000.0 + i, i, "M", "meteora_damm_v2", 300_000 * 10**9, 60 * L, None, None) for i in range(5)])
    data, skipped, _ = load(store.path if hasattr(store, "path") else store.db.execute("PRAGMA database_list").fetchone()[2],
                            1, {"meteora_damm_v2": 100}, 0.01, 3600)
    assert not data and skipped == 1                               # no calibration stored -> skipped
    store.record_factor("M", 999.0, 1.2)
    data, skipped, _ = load(store.db.execute("PRAGMA database_list").fetchone()[2], 1, {"meteora_damm_v2": 100}, 0.01, 3600)
    assert len(data) == 1 and data[0].series[0][3] == 1.2
    print("what-if: vault ticks scaled with stored calibrations, skipped without")


if __name__ == "__main__":
    asyncio.run(main())
    print("ALL PASSED")
