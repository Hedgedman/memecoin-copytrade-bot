"""reject_transfer_hook in pre_trade_check, on recorded mints: python -m tests.test_transfer_hook"""
import asyncio
import json
import os

from bot.chains.solana import SolanaAdapter, transfer_hook

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "mints_transfer_hook.json")


def adapter(account, **cfg):
    a = SolanaAdapter({"rpc_http": "http://x", "rpc_ws": "ws://x", "leaders": [], **cfg}, paper=True)
    calls = []

    async def rpc(method, params):            # the ONE getMultipleAccounts of _resolve; mint first
        calls.append(method)
        assert method == "getMultipleAccounts"
        return {"context": {"slot": 1}, "value": [account] + [None] * (len(params[0]) - 1)}

    async def jupiter(token, lamports, supply):
        return True, "passed to jupiter check"
    a._rpc, a._check_on_jupiter = rpc, jupiter
    return a, calls


async def main():
    m = json.load(open(FIX))
    info = lambda k: m[k]["account"]["data"]["parsed"]["info"]
    assert transfer_hook(info("hook")) == "9eQyvzp3hfghBhA4VUECLZzyZswmFGcne1rfs9QN3CnL"
    assert transfer_hook(info("t22_plain")) is None and transfer_hook(info("spl")) is None
    assert transfer_hook({"extensions": [{"extension": "transferHook",
                                          "state": {"authority": "X", "programId": None}}]}) == "authority"
    assert transfer_hook({"extensions": [{"extension": "transferHook",
                                          "state": {"authority": None, "programId": None}}]}) is None

    a, calls = adapter(m["hook"]["account"])
    ok, why = await a.pre_trade_check(m["hook"]["mint"], 0.5)
    assert not ok and why == "transfer hook set (sells can be taxed/blocked): 9eQyvzp3", why
    assert calls == ["getMultipleAccounts"]                 # no extra RPC call

    for k in ("t22_plain", "spl"):                          # pump.fun-style Token-2022 and plain SPL pass
        a, _ = adapter(m[k]["account"])
        ok, why = await a.pre_trade_check(m[k]["mint"], 0.5)
        assert ok and why == "passed to jupiter check", (k, why)

    a, _ = adapter(m["hook"]["account"], reject_transfer_hook=False)   # switchable, like the others
    ok, why = await a.pre_trade_check(m["hook"]["mint"], 0.5)
    assert ok, why
    print("hook mint rejected in the same getMultipleAccounts; Token-2022 without hook and SPL pass")


if __name__ == "__main__":
    asyncio.run(main())
    print("ALL PASSED")
