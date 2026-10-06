"""Offline tests for EVM detection: python -m tests.test_evm"""
from bot.chains.evm import parse_leader_tx, TRANSFER, pad

L = "0x" + "a" * 40; TOK = "0x" + "b" * 40; WN = "0x" + "c" * 40; PAIR = "0x" + "d" * 40

def tr(token, frm, to, amt):
    return {"address": token, "topics": [TRANSFER, pad(frm), pad(to)], "data": hex(amt)}

def test():
    E = 10**18
    # native buy through router: value sent, token in
    assert parse_leader_tx([tr(WN, PAIR, PAIR, E), tr(TOK, PAIR, L, 500)], L, E, WN) == ("buy", TOK.lower(), 500, 1.0)
    # bonding-curve buy (no wrapped native involved)
    assert parse_leader_tx([tr(TOK, PAIR, L, 900)], L, 2 * E, WN)[0] == "buy"
    # buy paid with wrapped native
    assert parse_leader_tx([tr(WN, L, PAIR, E // 2), tr(TOK, PAIR, L, 7)], L, 0, WN) == ("buy", TOK.lower(), 7, 0.5)
    # sell
    assert parse_leader_tx([tr(TOK, L, PAIR, 500), tr(WN, PAIR, PAIR, E)], L, 0, WN)[0] == "sell"
    # token arriving with no native paid (airdrop/transfer) -> ignored
    assert parse_leader_tx([tr(TOK, PAIR, L, 500)], L, 0, WN) is None
    # multi-token tx -> ignored
    assert parse_leader_tx([tr(TOK, PAIR, L, 1), tr("0x" + "e" * 40, PAIR, L, 1)], L, E, WN) is None
    print("evm parser ok")

if __name__ == "__main__":
    test()
