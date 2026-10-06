"""Trailing stop tests: python -m tests.test_trailing"""
import asyncio, tempfile, time
from bot.models import Fill, Position
from bot.positions import PositionManager
from bot.store import Store


class A:
    def __init__(self, path): self.path, self.i = path, 0
    async def quote_exit(self, p):
        v = self.path[min(self.i, len(self.path) - 1)]; self.i += 1; return v
    async def sell(self, p, slip=None):
        return Fill(True, None, p.token_amount, self.path[min(self.i - 1, len(self.path) - 1)])


async def run(path, **cfg):
    store = Store(tempfile.mktemp())
    base = {"take_profit": 0, "stop_loss": 0.10, "max_hold_s": 0, "stop_basis": "entry_price",
            "trailing_activate": 0.5, "trailing_pct": 0.3, "mark_interval_s": 0}
    base.update(cfg)
    a = A(path)
    pm = PositionManager(base, {"solana": a}, store)
    p = Position(None, "solana", "T", "L", 1, 1.0, time.time(), None, True)
    p.id = store.open_position(p); pm.add(p)
    for _ in path:
        if p.status == "closed":
            break
        await pm._check(p)
    return p, store


async def main():
    # runner: up to 3x, then falls >30% from the peak -> trailing stop at 2.05
    p, _ = await run([0.97, 1.2, 1.6, 2.4, 3.0, 2.6, 2.05, 1.0])
    assert p.exit_reason == "trailing_stop" and abs(p.exit_value - 2.05) < 1e-9, (p.exit_reason, p.exit_value)
    # never reaches +50%: normal stop at -10% from the first value
    p, _ = await run([0.97, 1.15, 1.3, 0.95, 0.86])
    assert p.exit_reason == "stop_loss", p.exit_reason
    # activated at +50%, then dumps: exits at the first value <= peak*0.7 = 1.05 (above break-even)
    p, _ = await run([0.97, 1.5, 1.2, 1.04, 0.5])
    assert p.exit_reason == "trailing_stop" and p.exit_value == 1.04, (p.exit_reason, p.exit_value)
    # once trailing is active the -10% stop no longer applies on its own
    p, _ = await run([1.0, 1.6, 1.3, 1.25])
    assert p.status == "open"
    # fixed take-profit still works if set (cap)
    p, _ = await run([0.97, 1.5, 2.0, 3.5], take_profit=2.0)
    assert p.exit_reason == "take_profit"
    # peak survives a restart (rebuilt from marks)
    p, store = await run([0.97, 1.6, 2.0])
    reloaded = store.load_open(True)[0]
    assert reloaded.peak_value == 2.0, reloaded.peak_value
    print("trailing stop ok (runner, no-activation stop, break-even floor, TP cap, restart)")


if __name__ == "__main__":
    asyncio.run(main())
    print("ALL PASSED")
