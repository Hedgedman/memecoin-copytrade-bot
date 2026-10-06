"""Offline tests for pool pricing: python -m tests.test_pricing"""
import asyncio, base64, struct, tempfile, time
from solders.pubkey import Pubkey

from bot.chains.solana import SolanaAdapter, _Throttle, LAMPORTS
from bot.models import Position
from bot.positions import PositionManager
from bot.pricing.pools import (PoolState, decode_curve, decode_token_amount, find_pool_vaults,
                               curve_pda, PUMP_PROGRAM, PUMPSWAP_PROGRAM, WSOL)
from bot.store import Store

L = LAMPORTS
FRESH = (1_073_000_000 * 10**6, 30 * L, 793_100_000 * 10**6, 0, 1_000_000_000 * 10**6, False)


def curve_bytes(f=FRESH):
    return b"\x00" * 8 + struct.pack("<QQQQQ?", *f) + b"\x01" * 32


def token_acct_bytes(amount):
    return b"\x00" * 64 + struct.pack("<Q", amount) + b"\x00" * 93


def test_math():
    st = decode_curve(curve_bytes(), slot=7)
    assert st.slot == 7 and st.venue == "pumpfun_curve"
    out = st.buy_out(2 * L, 0)
    assert 2 * L - 2 <= st.after_buy(2 * L, 0).sell_out(out, 0) <= 2 * L   # no-fee round trip ~lossless
    # the paper-mode bug this fixes: selling into the pool WITHOUT our buy loses 2x/(S+2x)
    mid = st.after_buy(10 * L, 0)                       # 40 virtual SOL in the curve
    phantom = 1 - mid.sell_out(mid.buy_out(2 * L, 0), 0) / (2 * L)
    assert abs(phantom - 4 / 44) < 1e-3, phantom
    late = st.after_buy(10 * L, 100).buy_out(2 * L, 100)
    assert late < 0.6 * st.buy_out(2 * L, 100)       # 10 SOL ahead of you: ~43% fewer tokens
    assert decode_token_amount(token_acct_bytes(12345)) == 12345
    for bad in (b"\x00" * 20,):
        try:
            decode_curve(bad); raise AssertionError
        except ValueError:
            pass
    print("math ok")


def test_find_vaults():
    leader, pool, other = "Leader", "PoolAcc", "UsdcPool"
    mint = "MintX"
    keys = [{"pubkey": k} for k in (leader, "lt", "lw", "tv", "sv", "uw", "uu")]
    def b(i, m, o, a): return {"accountIndex": i, "mint": m, "owner": o, "uiTokenAmount": {"amount": str(a)}}
    tx = {"meta": {
        "preTokenBalances": [b(1, mint, leader, 0), b(3, mint, pool, 1000), b(4, WSOL, pool, 50),
                             b(5, WSOL, other, 70), b(6, "USDC", other, 10)],
        "postTokenBalances": [b(1, mint, leader, 100), b(3, mint, pool, 900), b(4, WSOL, pool, 60),
                              b(5, WSOL, other, 60), b(6, "USDC", other, 20)]},
        "transaction": {"message": {"accountKeys": keys}}}
    assert find_pool_vaults(tx, mint, leader) == (pool, "tv", "sv")
    assert find_pool_vaults(tx, "OtherMint", leader) is None
    print("vault finder ok")


def make_adapter(**extra):
    cfg = {"rpc_http": "http://x", "rpc_ws": "ws://x", "leaders": [], "paper_fee_per_tx_sol": 0.01,
           "paper_land_delay_s": 0, "max_market_cap_usd": 0, "max_roundtrip_loss": 0.10, **extra}
    a = SolanaAdapter(cfg, paper=True)
    a._sol_usd_val, a._sol_usd_ts = 150.0, time.time() + 1e9
    return a


async def test_curve_flow():
    mint = str(Pubkey.new_unique())
    cpda = curve_pda(mint)
    a = make_adapter()
    calls = []

    async def fake_rpc(method, params):
        calls.append(method)
        assert method == "getMultipleAccounts"
        return {"context": {"slot": 100}, "value": [
            {"owner": "Tokenkeg", "data": {"parsed": {"info": {"supply": str(10**15),
                                                               "freezeAuthority": None, "mintAuthority": None}}}},
            {"owner": PUMP_PROGRAM, "data": [base64.b64encode(curve_bytes()).decode(), "base64"]}]}
    a._rpc = fake_rpc
    ticks = []; a.set_tick_sink(ticks.extend)
    ok, why = await a.pre_trade_check(mint, 2.0)
    assert ok and "pumpfun_curve" in why and calls == ["getMultipleAccounts"], why   # zero Jupiter calls
    fill = await a.buy(mint, 2.0)
    assert fill.ok and fill.model == "pumpfun_curve+0s" and fill.slot == 100
    p = Position(1, "solana", mint, "L", fill.token_amount, fill.quote_amount, time.time(), None, True)
    v0 = await a.quote_exit(p)
    # right after a paper buy, value ~ cost minus fees both ways (no phantom impact)
    assert 0.96 < v0 / 2.0 < 0.98, v0
    # someone buys 5 SOL after us -> pool update arrives over the websocket
    fired = []; a.set_price_callback(fired.append)
    bigger = a._pools[mint].state.after_buy(5 * L, 125)
    data = curve_bytes((bigger.tok_res, bigger.sol_res, bigger.real_tok, bigger.real_sol, 10**15, False))
    a._on_account(cpda, data, 101, time.time())
    v1 = await a.quote_exit(p)
    assert fired == [mint] and v1 > v0 * 1.2, (v0, v1)
    a._flush_ticks()
    assert len(ticks) >= 2 and ticks[-1][1] == 101
    # graduation: pool dropped, token marked graduated
    g = curve_bytes(FRESH[:5] + (True,))
    a._on_account(cpda, g, 102, time.time())
    assert mint not in a._pools and mint in a._graduated
    print("curve flow ok (0 Jupiter calls for check, fill and marks)")


async def test_pumpswap_flow():
    mint = str(Pubkey.new_unique())
    a = make_adapter()
    a._hints[mint] = ("PoolAcc", "TV", "SV")

    async def fake_rpc(method, params):
        keys = params[0]
        assert keys[2:] == ["PoolAcc", "TV", "SV"]
        tok = lambda amt: {"owner": "Tokenkeg", "data": {"parsed": {"info": {"tokenAmount": {"amount": str(amt)}}}}}
        return {"context": {"slot": 200}, "value": [
            {"owner": "Tokenkeg", "data": {"parsed": {"info": {"supply": str(10**15)}}}},
            None,                                    # no bonding curve (graduated / not pump)
            {"owner": PUMPSWAP_PROGRAM, "data": ["", "base64"]}, tok(200_000_000 * 10**6), tok(100 * L)]}
    a._rpc = fake_rpc
    ok, why = await a.pre_trade_check(mint, 2.0)
    assert ok and "pumpswap_amm" in why, why
    p = Position(1, "solana", mint, "L", a._pools[mint].state.buy_out(2 * L, 30), 2.01, time.time(), None, True)
    v0 = await a.quote_exit(p)
    # half-updated pool must NOT be priced: SOL vault moves first, token vault 20 ms later
    a._on_account("SV", token_acct_bytes(110 * L), 201, time.time())
    assert await a.quote_exit(p) == v0, "priced a half-updated pool"
    a._on_account("TV", token_acct_bytes(181_818_182 * 10**6), 201, time.time())
    v1 = await a.quote_exit(p)
    assert v1 > v0 * 1.15, (v0, v1)
    print("pumpswap flow ok (half-updates ignored)")


async def test_throttle_priority():
    t = _Throttle(max_rps=2.0)          # 0.5 s per slot
    order = []
    async def bg(i):
        await t.wait(); order.append(f"bg{i}")
    async def hi():
        await asyncio.sleep(0.05); await t.wait(priority=True); order.append("hi")
    await asyncio.gather(*(bg(i) for i in range(4)), hi())
    assert order.index("hi") <= 1, order   # entry path jumps the queue of exit quotes
    print("throttle priority ok", order)


async def test_tick_triggers_exit():
    class A:
        def __init__(self): self.value = 1.0
        async def quote_exit(self, p): return self.value
        async def sell(self, p, slip=None):
            from bot.models import Fill
            return Fill(True, None, p.token_amount, self.value, slot=5, model="test")
    store = Store(tempfile.mktemp()); a = A()
    pm = PositionManager({"take_profit": 0.3, "stop_loss": 0.1, "max_hold_s": 0, "stop_basis": "entry_price"},
                         {"solana": a}, store)
    p = Position(None, "solana", "T", "L", 1, 1.0, time.time(), None, True)
    p.id = store.open_position(p); pm.add(p)
    await pm._check(p)                       # sets ref
    a.value = 0.85
    pm.on_tick("solana", "T"); await asyncio.sleep(0.01)
    assert p.status == "closed" and p.exit_reason == "stop_loss"
    assert p.timing["exit_trigger_value"] == 0.85 and p.timing["exit_model"] == "test"
    row = store.db.execute("SELECT timing FROM positions WHERE id=?", (p.id,)).fetchone()[0]
    assert "exit_fill_slot" in row
    store.record_ticks([(1.0, 1, "T", "pumpfun_curve", 10, 20, 5, 6)])
    assert store.db.execute("SELECT COUNT(*) FROM ticks").fetchone()[0] == 1
    print("tick-driven exit + timing storage ok")


async def test_hub_messages():
    from bot.pricing.pools import AccountHub
    got = []
    h = AccountHub("ws://x", lambda k, d, slot, ts: got.append((k, d, slot)))
    h._wanted.add("Acc1"); h._pending[7] = "Acc1"
    h._handle({"jsonrpc": "2.0", "id": 7, "result": 42})
    h._handle({"method": "accountNotification", "params": {"subscription": 42, "result": {
        "context": {"slot": 9}, "value": {"data": [base64.b64encode(b"hi").decode(), "base64"]}}}})
    h._handle({"method": "accountNotification", "params": {"subscription": 99, "result": {
        "context": {"slot": 9}, "value": None}}})            # unknown subscription: ignored
    assert got == [("Acc1", b"hi", 9)], got
    print("hub message handling ok")


if __name__ == "__main__":
    test_math(); test_find_vaults()
    asyncio.run(test_curve_flow()); asyncio.run(test_pumpswap_flow())
    asyncio.run(test_throttle_priority()); asyncio.run(test_tick_triggers_exit())
    asyncio.run(test_hub_messages())
    print("ALL PASSED")
