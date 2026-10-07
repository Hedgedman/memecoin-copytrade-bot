"""What-if leader-exit style: python -m tests.test_whatif_leader"""
import os
import sqlite3
import tempfile

from bot.pricing.pools import PoolState
from whatif_ticksv2 import L, Pos, leader_exit_ts, run, venue_class, venue_split

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


def venue_tests():
    import contextlib, io, json
    assert venue_class({"fill_model": "pumpswap_amm+0.8s"}, None) == "pool"
    assert venue_class({"fill_model": "pumpfun_curve+0.8s"}, None) == "pool"
    assert venue_class({"fill_model": "meteora_damm_v2+0.8s"}, None) == "vault"
    assert venue_class({"fill_model": "jupiter+0.8s"}, None) == "jupiter"
    assert venue_class({}, None) == "jupiter"                                  # no model, no ticks
    assert venue_class({}, pos([(0, 1.0)], None)) == "pool"                    # old position: from its ticks
    path = tempfile.mktemp()
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE positions (id, chain, token, leader, cost, exit_value, opened_at, exit_reason, timing, "
               "paper, status)")
    rows = [(1, 1.0, 1.3, "take_profit", "pumpswap_amm+0.8s"), (2, 1.0, 0.88, "stop_loss", "pumpswap_amm+0.8s"),
            (3, 1.0, 0.75, "stop_loss", "jupiter+0.8s")]
    db.executemany("INSERT INTO positions VALUES (?, 'solana', 'T', 'L', ?, ?, 0, ?, ?, 1, 'closed')",
                   [(i, c, e, r, json.dumps({"fill_model": m})) for i, c, e, r, m in rows])
    db.commit(); db.close()
    out = io.StringIO()
    p1 = pos([(0, 1.0), (10, 1.5)], None); p1.pid = 1
    with contextlib.redirect_stdout(out):
        venue_split(path, 0, False, [p1], None, None, [("fixed", ("fixed", 0.3, 0, 0.2))], 0.0, 0.0,
                    "entry_price", 0.5, 0.0, 0.25)
    lines = {l.split()[0]: l for l in out.getvalue().splitlines() if l.strip().split(" ")[0] in ("pool", "jupiter")}
    assert lines["pool"].split()[1:4] == ["2", "1", "+0.18"], lines["pool"]
    assert lines["jupiter"].split()[1:4] == ["1", "0", "-0.25"] and "no ticks" in lines["jupiter"], lines["jupiter"]
    assert "1/1" in lines["pool"]                                               # what-if on the replayable one
    os.remove(path)
    print("venue split: pool / vault / jupiter from the fill model; actual results + what-if per class")


if __name__ == "__main__":
    main()
    venue_tests()
    print("ALL PASSED")
