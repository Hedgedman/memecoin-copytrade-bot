"""Block 1 data-quality guards (Oct 8 2026): python -m tests.test_data_quality

1.1 pump.fun curve quote mint (real 151-byte account of 9TPaz...: quote J3NK..., not SOL) -> no curve pool
1.2 fill sanity guard: pool model price vs the leader's fill price outside 0.5-3x -> Jupiter for check and fill
1.3 source switch: first quote of a new source > 2x off the last mark -> flagged, no exit until confirmed
1.4 stale-state guard: a silent pool is re-read over HTTP; a graduated curve found that way -> Jupiter
    + a pool stays watched while another open position still holds the token"""
import asyncio
import base64
import struct
import tempfile
import time

from solders.pubkey import Pubkey

from bot.chains.solana import LAMPORTS
from bot.models import Fill, Position
from bot.positions import PositionManager
from bot.pricing.pools import PUMP_PROGRAM, WSOL, curve_pda, curve_quote_mint, decode_curve
from bot.store import Store
from tests.test_pricing import FRESH, curve_bytes, make_adapter

L = LAMPORTS
# getAccountInfo of the bonding curve of 9TPazfktQJR5MyrdXNLy9WixRs9tZFCPtnjxoFfyUhWg (Oct 8 2026, 151 bytes)
CURVE_9TPAZ = bytes.fromhex(
    "17b7f83760d8ac6000000000000000000000000000000000000000000000000000000000000000000080c6a47e8d0300019ae9d6"
    "50cc2e9eb01071397e0ab2b010e78296bf02e1cb954645bbdfa5dcc38c0000fd31f4300ca5b00a3e8247b6aad54d9f7bd61b267f"
    "608998cc5fe9f2d97c3d45000000000000000000010000000000000000000000000000000000000000000000000000")
QUOTE_9TPAZ = "J3NKxxXZcnNiMjKw9hYb2K4LUxgwB6t1FtPtQVsv3KFr"


def foreign_curve_bytes(f=FRESH, quote=QUOTE_9TPAZ):
    """A live (not complete) curve in the new layout with a non-SOL quote mint."""
    return (b"\x00" * 8 + struct.pack("<QQQQQ?", *f) + b"\x01" * 32 + b"\x00\x00"
            + bytes(Pubkey.from_string(quote)) + b"\x00" * 36)


def test_decode():
    assert len(CURVE_9TPAZ) == 151
    assert curve_quote_mint(CURVE_9TPAZ) == QUOTE_9TPAZ
    st = decode_curve(CURVE_9TPAZ)
    assert st.quote_mint == QUOTE_9TPAZ and st.complete
    # legacy layouts (no quote mint) and an explicit WSOL / zero quote mint are SOL-quoted
    assert curve_quote_mint(curve_bytes()) is None and decode_curve(curve_bytes()).quote_mint is None
    assert curve_quote_mint(foreign_curve_bytes(quote=WSOL)) is None
    assert curve_quote_mint(b"\x00" * 8 + struct.pack("<QQQQQ?", *FRESH) + b"\x01" * 32 + b"\x00" * 70) is None
    assert decode_curve(foreign_curve_bytes()).quote_mint == QUOTE_9TPAZ
    print("1.1 decode: quote mint at offset 83 (IDL), 9TPaz curve -> J3NK..., legacy/WSOL/zero -> SOL")


def rpc_with(curve_data, supply=10**15):
    calls = []

    async def fake_rpc(method, params):
        calls.append(method)
        if method == "getMultipleAccounts":
            if len(params[0]) == 1:                      # _refresh: the curve account only
                return {"context": {"slot": 300}, "value": [
                    {"owner": PUMP_PROGRAM, "data": [base64.b64encode(curve_data[0]).decode(), "base64"]}]}
            return {"context": {"slot": 100}, "value": [
                {"owner": "Tokenkeg", "data": {"parsed": {"info": {"supply": str(supply),
                                                                   "freezeAuthority": None, "mintAuthority": None}}}},
                {"owner": PUMP_PROGRAM, "data": [base64.b64encode(curve_data[0]).decode(), "base64"]}]}
        raise AssertionError(method)
    return fake_rpc, calls


def jupiter(a, out_tokens=10**12, out_lamports=None):
    """Fake Jupiter: buy quotes give out_tokens, sell quotes give out_lamports (default: ~what was spent)."""
    quotes = []

    async def fake_quote(in_mint, out_mint, amount, slippage_bps=None, priority=False):
        quotes.append((in_mint, out_mint))
        if in_mint == WSOL:
            return {"outAmount": str(out_tokens)}
        return {"outAmount": str(out_lamports if out_lamports is not None else int(1.9 * L))}
    a._quote = fake_quote
    return quotes


async def test_foreign_quote():
    mint = str(Pubkey.new_unique())
    a = make_adapter()
    a._rpc, calls = rpc_with([foreign_curve_bytes()])
    q = jupiter(a)
    ok, why = await a.pre_trade_check(mint, 2.0)
    assert ok and "jupiter" in why and mint in a._foreign_quote and mint not in a._pools, why
    fill = await a.buy(mint, 2.0)
    assert fill.model == "jupiter+0s" and fill.token_amount == 10**12
    assert q[0][1] == mint and len(q) == 3                      # check: buy+sell quote, fill: buy quote
    await a.track(mint)
    assert mint not in a._pools                                 # never registered as a pool
    # a curve that turns foreign while watched (update over the websocket) is dropped
    mint2 = str(Pubkey.new_unique())
    b = make_adapter()
    b._rpc, _ = rpc_with([curve_bytes()])
    ok, why = await b.pre_trade_check(mint2, 2.0)
    assert ok and "pumpfun_curve" in why and mint2 in b._pools
    b._on_account(curve_pda(mint2), foreign_curve_bytes(), 101, time.time())
    assert mint2 not in b._pools and mint2 in b._foreign_quote
    print("1.1 adapter: non-SOL quote curve -> no pool, check + fill via Jupiter, dropped if it turns foreign")


async def test_fill_sanity():
    mint = str(Pubkey.new_unique())
    a = make_adapter()
    a._rpc, _ = rpc_with([curve_bytes()])
    st = decode_curve(curve_bytes())
    model = st.lamports_per_raw()
    jupiter(a)
    # leader paid about the model price (1.2x): pool model accepted
    ok, why = await a.pre_trade_check(mint, 2.0, ref_price=model / 1.2)
    assert ok and "pumpfun_curve" in why and mint in a._pools, why
    fill = await a.buy(mint, 2.0)
    assert fill.model == "pumpfun_curve+0s"
    # position 299: the model priced 32x the leader's fill -> rejected, Jupiter for check and fill
    mint2 = str(Pubkey.new_unique())
    b = make_adapter()
    b._rpc, _ = rpc_with([curve_bytes()])
    events = []
    b.set_event_sink(lambda *e: events.append(e))
    jupiter(b)
    ok, why = await b.pre_trade_check(mint2, 2.0, ref_price=model / 32)
    assert ok and "jupiter" in why and mint2 in b._model_rejected and mint2 not in b._pools, why
    assert events and events[0][3] == "model_rejected"
    fill = await b.buy(mint2, 2.0)
    assert fill.model == "jupiter+0s"
    await b.track(mint2)
    assert mint2 not in b._pools                                # stays rejected
    # too cheap (model 0.4x his price) is rejected as well
    mint3 = str(Pubkey.new_unique())
    c = make_adapter()
    c._rpc, _ = rpc_with([curve_bytes()])
    jupiter(c)
    ok, why = await c.pre_trade_check(mint3, 2.0, ref_price=model / 0.4)
    assert ok and "jupiter" in why and mint3 in c._model_rejected
    # paper fill guard on its own: the reference arrives after the pool was registered by observe()
    mint4 = str(Pubkey.new_unique())
    d = make_adapter()
    d._rpc, _ = rpc_with([curve_bytes()])
    jupiter(d)
    await d.observe(mint4)
    assert mint4 in d._pools
    d._ref_price[mint4] = model / 10
    fill = await d.buy(mint4, 2.0)
    assert fill.model == "jupiter+0s" and mint4 not in d._pools
    print("1.2 fill sanity: model within 0.5-3x of his price -> pool; 32x / 0.4x -> rejected, Jupiter check + fill")


class Src:
    """Adapter stand-in with a quote source: (source, value) per call."""
    def __init__(self, path):
        self.path, self.i, self.src = path, 0, None
    async def quote_exit(self, p):
        self.src, v = self.path[min(self.i, len(self.path) - 1)]
        self.i += 1
        return v
    def quote_source(self, token):
        return self.src
    async def sell(self, p, slip=None):
        return Fill(True, None, p.token_amount, self.path[min(self.i - 1, len(self.path) - 1)][1])


async def switch_run(path, sleep=0.0, **cfg):
    store = Store(tempfile.mktemp())
    base = {"take_profit": 0.0, "stop_loss": 0.2, "max_hold_s": 0, "stop_basis": "entry_price", "mark_interval_s": 0,
            "switch_confirm_s": 0.05}
    pm = PositionManager({**base, **cfg}, {"solana": Src(path)}, store)
    p = Position(None, "solana", "T", "L", 1000, 1.0, time.time(), None, True)
    p.id = store.open_position(p); pm.add(p)
    for _ in path:
        if p.status == "closed":
            break
        await pm._check(p)
        if sleep:
            await asyncio.sleep(sleep)
    return p, pm


async def test_source_switch():
    # graduation (position 299): curve 1.95 -> first Jupiter quote 0.07: no stop on it; a Jupiter quote 2 s later
    # at 1.9 (the real market) -> nothing happens, ref_value untouched
    path = [("pumpfun_curve", 1.0), ("pumpfun_curve", 1.95), ("jupiter", 0.07), ("jupiter", 1.9), ("jupiter", 1.9)]
    p, pm = await switch_run(path, sleep=0.06)
    assert p.status == "open" and p.ref_value == 1.0, (p.status, p.ref_value)
    assert p.timing["source_switch"] == round(0.07 / 1.95, 4) and "pumpfun_curve->jupiter" in p.timing["flagged"]
    assert p.timing["quote_source"] == "jupiter"
    marks = [v for (v,) in pm.store.db.execute("SELECT value FROM marks WHERE position_id=? ORDER BY ts", (p.id,))]
    assert marks == [1.0, 1.95, 0.07, 1.9, 1.9], marks                              # the jump IS recorded
    # the same switch, but the new source confirms the drop -> stop on the confirmed value
    path = [("pumpfun_curve", 1.0), ("pumpfun_curve", 1.95), ("jupiter", 0.07), ("jupiter", 0.08)]
    p, _ = await switch_run(path, sleep=0.06)
    assert p.status == "closed" and p.exit_reason == "stop_loss" and p.exit_value == 0.08, (p.exit_reason, p.exit_value)
    # second quote too early (< confirm window): still waiting; then confirmed
    path = [("pumpfun_curve", 1.0), ("jupiter", 0.3), ("jupiter", 0.3), ("jupiter", 0.3)]
    p, pm = await switch_run(path, switch_confirm_s=0.5)
    assert p.status == "open" and p.pending_switch is not None
    await asyncio.sleep(0.55)
    await pm._check(p)
    assert p.status == "closed" and p.exit_reason == "stop_loss"
    # a switch within 2x is not flagged and acts at once; a TP on an unconfirmed jump up is held back too
    path = [("pumpfun_curve", 1.0), ("jupiter", 0.75)]
    p, _ = await switch_run(path)
    assert p.status == "closed" and "flagged" not in p.timing and p.timing["quote_source"] == "jupiter"
    path = [("pumpswap_amm", 1.0), ("jupiter", 3.0), ("jupiter", 0.9)]
    p, _ = await switch_run(path, sleep=0.06, take_profit=0.3)
    assert p.status == "open" and p.timing["source_switch"] == 3.0 and p.peak_value == 1.0
    # restart: last_value and quote_source come back, so the first quote after a restart is checked too
    store = Store(tempfile.mktemp())
    q = Position(None, "solana", "T", "L", 1000, 1.0, time.time(), None, True, timing={"quote_source": "pumpfun_curve"})
    q.id = store.open_position(q); store.mark(q, 1.8)
    r = store.load_open(True)[0]
    assert r.last_value == 1.8 and r.timing["quote_source"] == "pumpfun_curve"
    pm = PositionManager({"take_profit": 0.3, "stop_loss": 0.2, "max_hold_s": 0, "switch_confirm_s": 0.05},
                         {"solana": Src([("jupiter", 0.5), ("jupiter", 0.5)])}, store)
    pm.add(r)
    await pm._check(r)
    assert r.status == "open" and r.pending_switch and "flagged" in r.timing
    # an adapter without quote_source: the guard is inert
    p, _ = await switch_run([(None, 1.0), (None, 0.3)])
    assert p.status == "closed" and "quote_source" not in p.timing
    print("1.3 source switch: flagged + no exit on the first off quote, confirmed 2 s later, ref kept, "
          "marks recorded, restart-safe, inert without a source")


async def test_stale_guard():
    mint = str(Pubkey.new_unique())
    a = make_adapter(pool_stale_s=0.2)
    graduated = curve_bytes(FRESH[:5] + (True,))
    curve_data = [curve_bytes()]
    a._rpc, calls = rpc_with(curve_data)
    jupiter(a, out_lamports=int(0.8 * L))
    ok, _ = await a.pre_trade_check(mint, 2.0)
    fill = await a.buy(mint, 2.0)
    p = Position(1, "solana", mint, "L", fill.token_amount, fill.quote_amount, time.time(), None, True)
    v0 = await a.quote_exit(p)
    assert a.quote_source(mint) == "pumpfun_curve" and calls.count("getMultipleAccounts") == 1
    # fresh state: no re-read
    assert await a.quote_exit(p) == v0 and calls.count("getMultipleAccounts") == 1
    # stale (no tick for > pool_stale_s) and the account is unchanged on chain: re-read once, same value
    await asyncio.sleep(0.25)
    assert await a.quote_exit(p) == v0 and calls.count("getMultipleAccounts") == 2
    assert await a.quote_exit(p) == v0 and calls.count("getMultipleAccounts") == 2   # throttled
    # position 192: the curve graduated but no websocket update came -> the re-read finds it complete
    curve_data[0] = graduated
    await asyncio.sleep(0.25)
    v = await a.quote_exit(p)
    assert mint not in a._pools and mint in a._graduated and a.quote_source(mint) == "jupiter"
    assert abs(v - 0.8) < 1e-9, v
    # a re-read that fails is not a mark
    b = make_adapter(pool_stale_s=0.2)
    b._rpc, calls_b = rpc_with([curve_bytes()])
    await b.pre_trade_check(mint, 2.0)
    fb = await b.buy(mint, 2.0)
    pb = Position(2, "solana", mint, "L", fb.token_amount, fb.quote_amount, time.time(), None, True)
    assert await b.quote_exit(pb)
    async def down(method, params):
        raise RuntimeError("rpc down")
    b._rpc = down
    await asyncio.sleep(0.25)
    assert await b.quote_exit(pb) is None
    print("1.4 stale guard: silent pool re-read over HTTP (throttled), graduation found -> Jupiter, failed read = no mark")


async def test_release_shared_pool():
    released = []

    class A:
        async def quote_exit(self, p): return 0.5
        async def sell(self, p, slip=None): return Fill(True, None, p.token_amount, 0.5)
        async def release(self, token): released.append(token)
        async def track(self, token): pass
    store = Store(tempfile.mktemp())
    pm = PositionManager({"take_profit": 0.3, "stop_loss": 0.2, "max_hold_s": 0}, {"solana": A()}, store)
    p1 = Position(None, "solana", "T", "L1", 10, 1.0, time.time(), None, True)
    p2 = Position(None, "solana", "T", "L2", 10, 1.0, time.time(), None, True)
    for p in (p1, p2):
        p.id = store.open_position(p); pm.add(p)
    await pm.close(p1, "test")
    assert released == [] and p1.status == "closed"            # p2 still holds T: pool kept
    await pm.close(p2, "test")
    assert released == ["T"]
    print("release: pool kept while another open position holds the token")


def main():
    test_decode()
    asyncio.run(test_foreign_quote())
    asyncio.run(test_fill_sanity())
    asyncio.run(test_source_switch())
    asyncio.run(test_stale_guard())
    asyncio.run(test_release_shared_pool())
    print("ALL PASSED")


if __name__ == "__main__":
    main()
