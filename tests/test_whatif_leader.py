"""What-if leader-exit style: python -m tests.test_whatif_leader"""
import os
import sqlite3
import tempfile

from bot.pricing.pools import PoolState
from whatif_ticksv2 import L, Pos, leader_exit_ts, run

base = PoolState("pumpswap_amm", 10**12, 100 * L)
K = base.sol_res * base.tok_res


def at(m):                                     # pool at a price multiple (constant k)
    sol = int(base.sol_res * m ** 0.5)
    return PoolState("pumpswap_amm", K // sol, sol)


TOKENS = base.buy_out(int(0.5 * L), 0)
COST = base.sell_out(TOKENS, 0) / L


def pos(path, leader_exit):
    return Pos(1, COST, TOKENS, 0, [(t, at(m), 0, 1.0) for t, m in path], leader_exit=leader_exit)


def ret(p, sl=0.0, a=0.0, b=0.0, delay=0.0):
    v, c = run(p, "leader", a, b, sl, delay, 0.0, "entry_price", 0.5)
    return round(v / COST - 1, 2), c


def main():
    # dips -30%, then the leader sells at t=7.5 while it is +50%; it later dumps
    path = [(0, 1.0), (2, 0.7), (4, 1.2), (7, 1.5), (8, 1.5), (9, 1.6), (12, 0.4)]
    assert ret(pos(path, 7.5)) == (0.5, False)               # fills at first tick >= 7.5 s (t=8: 1.5x)
    assert ret(pos(path, 7.5), delay=2.0) == (-0.6, False)   # + 2 s delay -> t=12 tick (0.4x)
    assert ret(pos(path, 7.5), sl=0.2) == (-0.3, False)      # -20% safety stop fires in the dip
    assert ret(pos(path, 7.5), sl=0.4) == (0.5, False)       # wide stop survives the dip
    assert ret(pos(path, None)) == (-0.6, True)              # leader never sold: censored at the last tick
    assert ret(pos(path, 30)) == (-0.6, True)                # sold after the data ends: censored too
    assert ret(pos(path, None), a=0.5, b=0.3) == (-0.6, False)   # trail 30% after +50% fires at t=12 (0.4 < 1.12)
    up = [(0, 1.0), (1, 1.6), (2, 1.0), (3, 1.0), (10, 2.0)]
    assert ret(pos(up, 9), a=0.5, b=0.3) == (0.0, False)     # trailing fires before the leader sells
    assert ret(pos(up, 9)) == (1.0, False)                   # leader exit alone holds to t=10
    assert ret(pos([(5, 1.0), (6, 1.3)], 1.0)) == (0.0, False)   # leader sold before our first tick
    print("leader exit: fill timing, delay, safety stop, censoring, combined trailing")

    path = tempfile.mktemp()
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE signals (ts, leader, side, token)")
    db.executemany("INSERT INTO signals VALUES (?,?,?,?)", [
        (90, "L", "sell", "T"),                        # before our entry: ignored
        (110, "X", "sell", "T"),                       # other leader: ignored
        (120, "L", "buy", "T"),                        # add-on buy: ignored
        (150, "L", "sell", "T"), (200, "L", "sell", "T")])
    assert leader_exit_ts(db, "T", "L", 100) == 150
    assert leader_exit_ts(db, "T", "L", 300) is None
    db.close(); os.remove(path)
    print("leader_exit_ts: first sell by the same leader after entry")


if __name__ == "__main__":
    main()
    print("ALL PASSED")
