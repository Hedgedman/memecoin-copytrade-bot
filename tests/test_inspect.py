"""scripts/inspect_wallet.py classifier on recorded transactions: python -m tests.test_inspect"""
import json
import os

from bot.chains.solana import parse_stable_swap
from scripts.inspect_wallet import classify

W = "Leader-G"
FIX = os.path.join(os.path.dirname(__file__), "fixtures")


def load(name):
    with open(os.path.join(FIX, name)) as f:
        return json.load(f)


def main():
    buy = load("tx_v1_usdc_buy_Leader-G.json")            # version-1 tx, he signs with the Fomo co-signer
    assert buy["version"] == 1
    r = classify(buy, W)
    assert r["cls"] == "a_signer" and r["in_keys"] and r["cosigned_by_fomo"], r
    assert r["swap"][0] == "buy" and r["swap"][4] == "USD" and abs(r["swap"][3] - 1200) < 0.01, r["swap"]
    assert r["bot_parses"] and parse_stable_swap(buy, W)[0] == "buy"   # parsers handle v1 fine

    dep = classify(load("tx_relay_deposit_Leader-G.json"), W)
    assert dep["cls"] == "d_relay" and dep["relay"] == "deposit" and dep["signer"], dep

    other = classify(load("tx_unsigned_sol_in_Leader-G.json"), W)
    assert other["cls"] == "e_other" and not other["signer"] and other["swap"] is None, other
    print("classes:", r["cls"], dep["cls"], other["cls"])


if __name__ == "__main__":
    main()
    print("ALL PASSED")
