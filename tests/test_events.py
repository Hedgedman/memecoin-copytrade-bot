"""Dropped leader transactions are recorded: python -m tests.test_events"""
import asyncio, os, subprocess, sys, tempfile
from bot.chains.solana import SolanaAdapter
from bot.store import Store
from tests.test_core import tx, W, TOKEN

L = 1_000_000_000


async def main():
    path = tempfile.mktemp()
    store = Store(path)
    a = SolanaAdapter({"rpc_http": "http://x", "rpc_ws": "ws://x", "leaders": [W], "min_leader_buy_sol": 0.5}, paper=True)
    a.set_event_sink(store.record_event)
    q = asyncio.Queue()
    small = tx(5 * L, int(4.8 * L), None, 1000); small["transaction"]["message"]["accountKeys"][0]["signer"] = True
    await a._handle("SigSmall", W, q, 1.0, tx=small)                        # 0.2 SOL buy
    unsigned = tx(5 * L, 4 * L, None, 1000)                                  # leader didn't sign
    await a._handle("SigUnsigned", W, q, 1.0, tx=unsigned)
    weird = tx(5 * L, 5 * L); weird["transaction"]["message"]["accountKeys"][0]["signer"] = True
    await a._handle("SigWeird", W, q, 1.0, tx=weird)                         # no swap in it
    kinds = [r[0] for r in store.db.execute("SELECT kind FROM leader_events ORDER BY rowid")]
    assert kinds == ["small_buy", "not_signer", "not_a_swap"], kinds
    assert q.empty()
    out = subprocess.run([sys.executable, os.path.abspath("why.py"), TOKEN], capture_output=True, text=True,
                         cwd=os.path.dirname(path), env={**os.environ, "PYTHONPATH": os.getcwd()})
    print("event kinds recorded:", kinds)


if __name__ == "__main__":
    asyncio.run(main())
    print("ALL PASSED")
