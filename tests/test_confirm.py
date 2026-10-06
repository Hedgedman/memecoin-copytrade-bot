"""Confirmed-stop tests (bot + what-if): python -m tests.test_confirm"""
import asyncio, tempfile, time
from bot.models import Fill, Position
from bot.positions import PositionManager
from bot.pricing.pools import PoolState
from bot.store import Store
from whatif_ticksv2 import Pos, run, L


class A:
    def __init__(self): self.value = 1.0
    async def quote_exit(self, p): return self.value
    async def sell(self, p, slip=None): return Fill(True, None, p.token_amount, self.value)


def manager(**cfg):
    base = {"take_profit": 0, "stop_loss": 0.10, "stop_basis": "entry_price", "max_hold_s": 0,
            "trailing_activate": 0.5, "trailing_pct": 0.3, "mark_interval_s": 0,
            "stop_confirm_s": 0.2, "hard_stop": 0.25}
    base.update(cfg)
    store, a = Store(tempfile.mktemp()), A()
    pm = PositionManager(base, {"solana": a}, store)
    p = Position(None, "solana", "T", "L", 1, 1.0, time.time(), None, True)
    p.id = store.open_position(p); pm.add(p)
    return pm, a, p


async def bot_tests():
    pm, a, p = manager()
    await pm._check(p)                         # reference = 1.0
    a.value = 0.88; await pm._check(p)         # below -10%: starts confirmation, no sale yet
    assert p.status == "open" and p.below_since is not None
    a.value = 0.97; await pm._check(p)         # recovers within 0.2 s
    assert p.status == "open" and p.below_since is None
    await asyncio.sleep(0.3); assert p.status == "open"   # pending re-check finds it recovered
    a.value = 0.87; await pm._check(p)         # dips again and STAYS there
    await asyncio.sleep(0.3)                   # re-check fires after 0.2 s without new ticks
    assert p.status == "closed" and p.exit_reason == "stop_loss", (p.status, p.exit_reason)

    pm, a, p = manager()
    await pm._check(p)
    a.value = 0.70; await pm._check(p)         # -30%: hard stop, no waiting
    assert p.exit_reason == "hard_stop"

    pm, a, p = manager(stop_confirm_s=0)
    await pm._check(p)
    a.value = 0.88; await pm._check(p)         # confirmation off: immediate, as before
    assert p.exit_reason == "stop_loss"
    print("bot: dip recovers -> held, sustained dip -> sold after window, crash -> hard stop")


def whatif_tests():
    base = PoolState("pumpswap_amm", 10**12, 100 * L)
    def at(mult):                              # pool at a price multiple (constant k)
        k = base.sol_res * base.tok_res
        sol = int(base.sol_res * mult ** 0.5)
        return PoolState("pumpswap_amm", k // sol, sol)
    tokens = base.buy_out(int(0.5 * L), 0)
    cost = base.sell_out(tokens, 0) / L
    def pos(path):                             # path: [(t, price multiple)]
        return Pos(1, cost, tokens, 0, [(t, at(m), 0, 1.0) for t, m in path])
    dip = pos([(0, 1.0), (1, 0.85), (1.5, 1.0), (5, 1.05), (10, 1.0)])     # 0.5 s bot dip
    v0, _ = run(dip, "fixed", 1.0, 0, 0.10, 0.0, 0.0, "entry_price", 0.5, 0, 0.25)
    v2, c2 = run(dip, "fixed", 1.0, 0, 0.10, 0.0, 0.0, "entry_price", 0.5, 2, 0.25)
    assert v0 < cost * 0.9 and c2 and v2 > cost * 0.95, (v0, v2)          # stopped vs held through
    fall = pos([(0, 1.0), (1, 0.85), (2, 0.84), (3, 0.83), (10, 0.5)])     # real decline
    v, c = run(fall, "fixed", 1.0, 0, 0.10, 0.0, 0.0, "entry_price", 0.5, 2, 0.25)
    assert not c and abs(v / cost - 0.83) < 0.02, v / cost                  # sold at the 3 s tick
    crash = pos([(0, 1.0), (1, 0.6), (2, 0.6)])
    v, _ = run(crash, "fixed", 1.0, 0, 0.10, 0.0, 0.0, "entry_price", 0.5, 5, 0.25)
    assert abs(v / cost - 0.6) < 0.02                                       # hard stop at once
    print("what-if: dip held through, real decline sold after confirmation, crash -> hard stop")


if __name__ == "__main__":
    asyncio.run(bot_tests())
    whatif_tests()
    print("ALL PASSED")
