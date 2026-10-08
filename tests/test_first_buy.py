"""Only-first-leader-buy filter: python -m tests.test_first_buy"""
import asyncio, tempfile, time
from bot.chains.solana import leader_token_balance, parse_wallet_swap
from bot.main import Bot
from bot.models import Signal
from tests.test_core import tx, W, TOKEN

L = 1_000_000_000


def test_balance():
    first = tx(5 * L, 4 * L, None, 1000)            # no token account before: first buy
    add = tx(5 * L, 4 * L, 3000, 4000)              # held 3000 before: add-on buy
    dust = tx(5 * L, 4 * L, 10, 1010)               # dust before (1% of buy): counts as first
    assert leader_token_balance(first, W, TOKEN) == 0
    assert leader_token_balance(add, W, TOKEN) == 3000
    assert parse_wallet_swap(add, W)[0] == "buy"
    return first, add, dust


async def test_bot_skips():
    first, add, dust = test_balance()
    cfg = {"paper": True, "db_path": tempfile.mktemp(), "chains": {"solana": {
        "enabled": True, "rpc_http": "http://x", "rpc_ws": "ws://x", "leaders": [W], "buy_amount": 0.5,
        "only_first_leader_buy": True, "max_signal_age_s": 999}},
        "risk": {"max_open_positions": 4, "reentry_cooldown_s": 0}, "exits": {}}
    bot = Bot(cfg)
    checked = []
    async def fake_check(token, amount, ref_price=None):
        checked.append(token); return False, "stop here"
    bot.adapters["solana"].pre_trade_check = fake_check
    for t, expect_checked in ((add, False), (first, True), (dust, True)):
        checked.clear()
        s = Signal("solana", W, "buy", TOKEN, 1000, 1.0, "sig", int(time.time()))
        s.leader_pre_amount = leader_token_balance(t, W, TOKEN)
        await bot.handle_buy(s)
        assert bool(checked) == expect_checked, (s.leader_pre_amount, checked)
    row = bot.store.db.execute("SELECT reason FROM signals WHERE action='skip' ORDER BY id LIMIT 1").fetchone()
    assert row and "add-on" in row[0], row
    print("add-on buys skipped, first buys and dust re-buys go through")


if __name__ == "__main__":
    test_balance()
    asyncio.run(test_bot_skips())
    print("ALL PASSED")
