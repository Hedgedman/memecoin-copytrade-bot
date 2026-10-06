"""Offline tests for transactionSubscribe detection: python -m tests.test_detection"""
import asyncio
from bot.chains.solana import tx_from_notification, signing_leader, parse_wallet_swap, SolanaAdapter
from tests.test_core import tx, W, TOKEN

L = 1_000_000_000


def helius_note(t, slot=123, sig="Sig1"):
    """Shape of a Helius transactionNotification result (jsonParsed, full details)."""
    inner = dict(t["transaction"]); inner["signatures"] = [sig]
    return {"signature": sig, "slot": slot, "transaction": {"transaction": inner, "meta": t["meta"]}}


def test_shape_and_parse():
    t = tx(5 * L, 4 * L, None, 1000)
    t["transaction"]["message"]["accountKeys"][0]["signer"] = True
    got = tx_from_notification(helius_note(t))
    assert got["slot"] == 123 and got["blockTime"] is None
    assert parse_wallet_swap(got, W) == ("buy", TOKEN, 1000, 1.0)       # parsers work unchanged
    assert signing_leader(got, {W}) == W
    assert signing_leader(got, {"Someone"}) is None
    t2 = tx(5 * L, 4 * L, None, 1000)                                   # leader mentioned, not signer
    assert signing_leader(tx_from_notification(helius_note(t2)), {W}) is None
    assert tx_from_notification({"signature": "x", "slot": 1, "transaction": "base64junk"}) is None
    print("notification shape + parsing ok")


async def test_handle_uses_pushed_tx():
    a = SolanaAdapter({"rpc_http": "http://x", "rpc_ws": "ws://x", "leaders": [W],
                       "min_leader_buy_sol": 0.5}, paper=True)
    async def no_rpc(*_):
        raise AssertionError("getTransaction must not be called when the tx was pushed")
    a._get_tx = no_rpc
    t = tx(5 * L, 4 * L, None, 1000)
    t["transaction"]["message"]["accountKeys"][0]["signer"] = True
    q = asyncio.Queue()
    await a._handle("Sig1", W, q, 1000.0, tx=tx_from_notification(helius_note(t)))
    s = q.get_nowait()
    assert (s.side, s.token, s.slot, s.notified_at) == ("buy", TOKEN, 123, 1000.0)
    print("pushed tx handled without getTransaction")


if __name__ == "__main__":
    test_shape_and_parse()
    asyncio.run(test_handle_uses_pushed_tx())
    print("ALL PASSED")
