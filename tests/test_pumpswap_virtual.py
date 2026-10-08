"""PumpSwap virtual_quote_reserves (pool math on vault + virtual): python -m tests.test_pumpswap_virtual

Fixture: the real DXvY5Ep1 pool account + the BuyEvent/SellEvent of our 4 live trades in it (pos 55/56, Oct 8 2026).
Before this fix the model priced on the quote vault alone and was 11-13% off vs Jupiter (live factor 1.119)."""
import asyncio, base64, json, os, time
from solders.pubkey import Pubkey

from bot.models import Position
from bot.pricing.pools import PUMPSWAP_PROGRAM, PoolState, decode_pumpswap_virtual, pumpswap_state
from tests.test_pricing import L, make_adapter, token_acct_bytes

FX = json.load(open(os.path.join(os.path.dirname(__file__), "fixtures", "pumpswap_pool_DXvY.json")))
MINT = "DXvY5Ep1j9Eqwf6qHkemgWgBsEfhEKDsSizDjT4ipump"
FEE = 100                                    # config pumpswap_fee_bps (real: lp 20 + protocol 5 + creator 95)


def pool_bytes(virtual: int) -> bytes:
    d = bytearray(base64.b64decode(FX["data_b64"]))
    d[245:261] = virtual.to_bytes(16, "little", signed=True)
    return bytes(d)


def test_decode():
    d = base64.b64decode(FX["data_b64"])
    assert FX["owner"] == PUMPSWAP_PROGRAM
    pk = lambda o: str(Pubkey.from_bytes(d[o:o + 32]))
    assert (pk(43), pk(139), pk(171)) == (MINT, FX["base_vault"], FX["quote_vault"])   # IDL offsets line up
    assert decode_pumpswap_virtual(d) == 17_584_369_132, decode_pumpswap_virtual(d)
    assert decode_pumpswap_virtual(pool_bytes(-123_456)) == -123_456                  # signed (fees kept in vault)
    assert decode_pumpswap_virtual(d[:245]) == 0                                      # legacy pool, no field
    st = pumpswap_state(100, 50 * L, -2 * L)
    assert st.sol_res == 48 * L and st.real_sol == 50 * L
    print("decode ok: virtual_quote_reserves = i128 at byte 245 (17.58 SOL on the fixture pool)")


def test_real_trades():
    """Model vs the program's own numbers for our 4 live trades: within 1% with virtual, >10% off without."""
    for name, e in FX["events"].items():
        b, q, v = e["pool_base_token_reserves"], e["pool_quote_token_reserves"], e["virtual_quote_reserves"]
        new, old = pumpswap_state(b, q, v), PoolState("pumpswap_amm", b, q)
        if e["event"] == "BuyEvent":
            real = e["base_amount_out"]
            got, was = new.buy_out(e["quote_amount_in"], FEE), old.buy_out(e["quote_amount_in"], FEE)
            exact = new.buy_out(e["user_quote_amount_in"], 0)         # net of all fees: exact up to rounding
        else:
            real = e["user_quote_amount_out"]
            got, was = new.sell_out(e["base_amount_in"], FEE), old.sell_out(e["base_amount_in"], FEE)
            exact = new.sell_out(e["base_amount_in"], 0)
            real_gross = e["quote_amount_out"]
            assert abs(exact - real_gross) <= 1, (name, exact, real_gross)
        assert abs(got / real - 1) < 0.01, (name, got / real)
        assert abs(was / real - 1) > 0.10, (name, was / real)
        if e["event"] == "BuyEvent":
            assert abs(exact / real - 1) < 1e-6, (name, exact, real)     # program rounding: ~1e-9
        print(f"  {name}: model/real {got / real:.4f} (vault-only model was {was / real:.4f})")
    print("real trades ok: model matches the program within 1%")


async def test_adapter_flow():
    """_resolve decodes the pool account; a pool-account update (v2 trade fee kept) re-prices."""
    e = FX["events"]["buy55"]
    b, q, v = e["pool_base_token_reserves"], e["pool_quote_token_reserves"], e["virtual_quote_reserves"]
    a = make_adapter(pumpswap_fee_bps=FEE)
    a._hints[MINT] = (FX["pool"], "TV", "SV")

    async def fake_rpc(method, params):
        assert params[0][2:] == [FX["pool"], "TV", "SV"]
        tok = lambda amt: {"owner": "Tokenkeg", "data": {"parsed": {"info": {"tokenAmount": {"amount": str(amt)}}}}}
        return {"context": {"slot": 300}, "value": [
            {"owner": "Tokenkeg", "data": {"parsed": {"info": {"supply": str(10**15)}}}}, None,
            {"owner": PUMPSWAP_PROGRAM, "data": [base64.b64encode(pool_bytes(v)).decode(), "base64"]},
            tok(b), tok(q)]}
    a._rpc = fake_rpc
    ok, why = await a.pre_trade_check(MINT, 1.0)
    assert ok and "pumpswap_amm" in why, why
    pool = a._pools[MINT]
    assert pool.state.sol_res == q + v and pool.state.real_sol == q and FX["pool"] in pool.accounts
    assert pool.state.buy_out(10**9, FEE) / e["base_amount_out"] - 1 < 0.01
    p = Position(1, "solana", MINT, "L", e["base_amount_out"], 1.0, time.time(), None, True)
    v0 = await a.quote_exit(p)
    # a v2 trade elsewhere keeps 0.5 SOL of fees in the vault: vault +0.5, virtual -0.5 -> price unchanged
    a._on_account("SV", token_acct_bytes(q + L // 2), 301, time.time())
    a._on_account("TV", token_acct_bytes(b), 301, time.time())
    a._on_account(FX["pool"], pool_bytes(v - L // 2), 301, time.time())
    assert pool.state.sol_res == q + v and pool.state.real_sol == q + L // 2
    assert abs(await a.quote_exit(p) - v0) < 1e-9
    # a sweep / boost change alone moves the price
    a._on_account(FX["pool"], pool_bytes(v + 10 * L), 302, time.time())
    assert pool.state.sol_res == q + L // 2 + v + 10 * L
    assert await a.quote_exit(p) > v0 * 1.05
    a._flush_ticks()
    print("adapter flow ok (pool account watched, fee-kept updates price-neutral)")


if __name__ == "__main__":
    test_decode(); test_real_trades(); asyncio.run(test_adapter_flow())
    print("ALL PASSED")
