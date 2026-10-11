"""whatif_ticksv2 leader / pool filters, PumpSwap virtual-reserve reconstruction and the ladder exit:
python -m tests.test_whatif_ladder

Synthetic ticks: a 100 SOL / 1e12 constant-product pool, moved to price multiples with k constant. A 0.5 SOL
position; mult = marginal price / our cost per token, so at(1.0) is ~1.01x (our buy sits in the shadow pool)."""
import contextlib
import io
import json
import os
import tempfile

from bot.pricing.pools import PoolState
from bot.store import Store
from whatif_ticksv2 import (ARMS, L, Ladder, LadderResult, Pos, VirtualSeries, arms_table, keep_leader, load,
                            marginal_mult, max_drawdown, parse_ladder, run_arm, run_ladder, with_virtual)

base = PoolState("pumpswap_amm", 10**12, 100 * L)
K = base.sol_res * base.tok_res


def at(m):
    sol = int(base.sol_res * m ** 0.5)
    return PoolState("pumpswap_amm", K // sol, sol)


TOKENS = base.buy_out(int(0.5 * L), 0)
COST = base.sell_out(TOKENS, 0) / L
H = 1 / 3600          # one second in hours (for hold_h)


def pos(path, pid=1, token="TOKEN", leader="LEADER"):
    # entry_net = our 0.5 SOL sits in the shadow pool (sells take it out again), as in the paper replay
    return Pos(pid, COST, TOKENS, int(0.5 * L), [(t, at(m), 0, 1.0) for t, m in path], token=token, leader=leader)


def lad(levels="2:0.25,3:0.25,5:0.25,9:0.25", **kw):
    return Ladder(parse_ladder(levels), **kw)


def go(path, ladder, delay=0.0, fee=0.0) -> LadderResult:
    return run_ladder(pos(path), ladder, delay, fee, "entry_price")


def ret(r: LadderResult):
    return round(r.value / COST - 1, 2)


def kinds(r: LadderResult):
    return [k for _, k, *_ in r.fills]


def test_ladder():
    up = [(0, 1.0), (1, 2.0), (2, 2.0), (3, 3.0), (4, 3.0), (5, 5.0), (6, 5.0), (7, 9.0), (8, 9.0), (9, 9.0)]
    r = go(up, lad())
    assert kinds(r) == ["2x", "3x", "5x", "9x"] and not r.censored and r.remainder == 0, r
    assert 3.70 <= ret(r) <= 3.80, ret(r)                      # 0.25 x (2 + 3 + 5 + 9) = 4.75x, minus impact
    assert all(abs(s - 0.25) < 0.01 for _, _, s, _, _ in r.fills)   # shares of the ORIGINAL position
    assert all(0 <= imp < 0.01 for *_, imp in r.fills)              # 0.125 SOL tranches: tiny impact
    print("ladder: four levels fill once each, 25% of the original position each, last level closes")

    r = go(up, lad(), delay=1.0)                                # fills on the first tick >= trigger + 1 s
    assert kinds(r) == ["2x", "3x", "5x", "9x"] and [t for t, *_ in r.fills] == [2, 4, 6, 8]
    r = go([(0, 1.0), (1, 2.0)], lad(), delay=1.0)               # no tick after the trigger: censored, nothing sold
    assert r.censored and r.remainder == 1.0 and r.fills == [] and round(r.value / COST, 1) == 2.0
    print("ladder: delay, censored before the first fill lands")

    r = go([(0, 1.0), (1, 2.0), (2, 1.5), (3, 2.0), (4, 2.0), (5, 1.5), (6, 1.5)], lad(after="none"))
    assert kinds(r) == ["2x"] and r.censored and abs(r.remainder - 0.75) < 0.01   # each level once; rest censored
    assert 0.25 * 2 + 0.75 * 1.5 - 0.1 < r.value / COST < 0.25 * 2 + 0.75 * 1.5 + 0.1
    print("ladder: a level fires once; remainder at the end = censored (valued at the last tick)")

    r = go([(0, 1.0), (1, 3.5), (2, 3.5)], lad())                # gap up through two levels: both fill at once
    assert kinds(r) == ["2x", "3x"] and r.censored and abs(r.remainder - 0.5) < 0.01
    r = go([(0, 1.0), (1, 3.5), (2, 3.5)], lad("2:0.5,3:0.5"))    # shares complete 1.0 -> closed
    assert kinds(r) == ["2x", "3x"] and not r.censored and 3.2 < ret(r) + 1 < 3.6     # both fill at the 3.5x tick
    r = go([(0, 1.0), (1, 3.5), (2, 3.5)], lad("2:0.3,3:0.3"))    # shares sum to 0.6: 40% stays (censored here)
    assert r.censored and abs(r.remainder - 0.4) < 0.01 and abs(r.fills[-1][2] - 0.3) < 0.01
    print("ladder: gap-up fills several levels on one tick; the level completing 1.0 sells what is left")

    dip = [(0, 1.0), (1, 0.9), (2, 0.75), (3, 0.75), (4, 2.5), (5, 2.5)]
    r = go(dip, lad())
    assert kinds(r) == ["stop"] and -0.27 < ret(r) < -0.23 and not r.censored   # -20% stop before any fill
    r = go(dip, lad(stop=0.30))
    assert kinds(r) == ["2x"]                                                  # wider stop survives the dip
    r = go(dip, lad(stop=0.0))
    assert kinds(r) == ["2x"]                                                  # 0 = no stop
    print("ladder: --ladder-stop X until the first fill (0 = none)")

    back = [(0, 1.0), (1, 2.0), (2, 2.0), (3, 0.95), (4, 0.95), (5, 0.7), (6, 0.7)]
    r = go(back, lad(after="entry"))
    assert kinds(r) == ["2x", "stop"] and r.fills[1][0] == 3 and 1.1 < r.value / COST < 1.3   # 0.5 + 0.75 x 0.95
    r = go(back, lad(after="none"))
    assert kinds(r) == ["2x"] and r.censored
    r = go(back, lad(after="0.8"))
    assert kinds(r) == ["2x", "stop"] and r.fills[1][0] == 5                  # 0.95 holds, 0.7 fires
    print("ladder: --stop-after-first entry | none | fraction")

    r = go(back, lad(after="none", hold_h=3 * H))
    assert kinds(r) == ["2x", "hold"] and r.fills[1][0] == 3 and not r.censored
    r = go([(0, 1.0), (1, 1.1), (2, 1.2), (3, 1.2), (4, 1.3)], lad(after="none", hold_h=2 * H))
    assert kinds(r) == ["hold"] and r.fills[0][0] == 2                        # max hold also without a fill
    print("ladder: --ladder-max-hold-h")

    tr = [(0, 1.0), (1, 2.0), (2, 2.0), (3, 4.0), (4, 3.5), (5, 2.7), (6, 2.7), (7, 1.0)]
    r = go(tr, lad("2:0.25", after="none", trail=0.30))
    assert kinds(r) == ["2x", "trail"] and r.fills[1][0] == 5 and 2.3 < r.value / COST < 2.7   # 4.0 -> 2.7 = -32%
    r = go(tr, lad("2:0.25", after="none", trail=0.40))
    assert kinds(r) == ["2x", "trail"] and r.fills[1][0] == 7                  # -40% only at the last tick
    r = go(tr, lad("", stop=0.2, after="none", trail=0.30, trail_act=3.0))
    assert kinds(r) == ["trail"] and r.fills[0][0] == 5                        # no levels: trail from 3x on
    r = go([(0, 1.0), (1, 1.5), (2, 1.0), (3, 1.0)], lad("", stop=0.2, after="none", trail=0.30, trail_act=3.0))
    assert r.censored and r.fills == []                                        # never activated
    print("ladder: --ladder-trail after the first fill or from --ladder-trail-act")

    try:
        parse_ladder("2:0.6,3:0.5")
        assert False
    except ValueError:
        pass
    assert parse_ladder("5:0.25, 2:0.5") == [(2.0, 0.5), (5.0, 0.25)]
    assert lad("2:0.25,3:0.75", stop=0.2, after="entry", trail=0.3, hold_h=24).label() == \
        "ladder 100%:25/200%:75, stop -20%, then entry, trail 30%, max 24h"
    assert max_drawdown([1, -2, -1, 3, -1]) == -3 and max_drawdown([]) == 0
    p = pos([(0, 1.0)])
    assert abs(marginal_mult(p, at(2.0), 0, 1.0, 0) / marginal_mult(p, at(1.0), 0, 1.0, 0) - 2.0) < 1e-6
    print("ladder: parse_ladder, label, max_drawdown, marginal_mult")


def test_arms():
    data = [pos([(0, 1.0), (1, 1.4), (2, 1.4), (3, 0.5)], 1, "AAAA"),             # A takes +30%; W1/L: the ladder's stop
            pos([(0, 1.0), (1, 2.2), (2, 3.2), (3, 3.2), (4, 0.2)], 2, "BBBB"),   # W1 +200%; L sells 2x+3x, rest at entry
            pos([(0, 1.0), (1, 1.1), (2, 1.2)], 3, "CCCC")]                         # ends while holding: censored in all
    assert run_arm(data[0], ARMS["A"], 0, 0, "entry_price")[0] / COST > 1.3
    v, c = run_arm(data[0], ARMS["W1"], 0, 0, "entry_price")
    assert not c and v / COST < 0.85
    v, c = run_arm(data[1], ARMS["L"], 0, 0, "entry_price")
    assert not c and 0.25 * 2 + 0.25 * 3 + 0.5 * 0.2 - 0.1 < v / COST < 1.5   # 2x, 3x, then the stop at entry fires at 0.2
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        res = arms_table(data, ARMS, 0.0, 0.0, "entry_price", title="T")
    text = out.getvalue()
    assert set(res["A"]["censored"]) == {3} and set(res["A"]["pnl"]) == {1, 2}
    assert "common set (uncensored in every arm) = 2" in text
    rowA = [l for l in text.splitlines() if l.strip().startswith("A ")][0]
    assert rowA.split()[1] == "2" and "1 (" in rowA and "AAAA" in rowA, rowA
    print("arms table: fixed and ladder arms, censored trades counted separately, drivers named")
    # HYPOTHESIS 11 (CHECKLIST_30_TRADES.md): the registered arms must not change
    reg = {"A": ("fixed", (0.30, 0.20)), "W1": ("fixed", (2.00, 0.20)), "W2": ("fixed", (4.00, 0.20)),
           "L": ("ladder", Ladder([(2.0, 0.25), (3.0, 0.25), (5.0, 0.25), (9.0, 0.25)], 0.20, "entry", 0.0, 0.0, 0.0)),
           "R1": ("ladder", Ladder([(1.3, 0.75), (3.0, 0.125), (5.0, 0.125)], 0.20, "0.8", 0.20, 3.0, 24)),
           "R2": ("ladder", Ladder([(1.5, 0.50), (3.0, 0.25), (5.0, 0.25)], 0.20, "entry", 0.20, 3.0, 24)),
           "R3": ("ladder", Ladder([(1.3, 0.75), (2.0, 0.125), (3.0, 0.125)], 0.20, "entry", 0.0, 0.0, 0.0))}
    assert {k: (v[0], v[1]) for k, v in ARMS.items()} == reg, "a registered H11 arm changed: add a new name instead"
    print("H11 arms registered as in CHECKLIST_30_TRADES.md")


def test_filters():
    assert keep_leader("ABCDEF", ("ABC",), ()) and not keep_leader("ABCDEF", ("XYZ",), ())
    assert not keep_leader("ABCDEF", (), ("AB",)) and keep_leader("ABCDEF", (), ("XY",)) and keep_leader("ABCDEF")
    path = os.path.join(tempfile.mkdtemp(), "t.db")
    db = Store(path).db
    T0 = 1_700_000_000.0
    rows = [(230, "LEADER_A", "pumpswap_amm"), (231, "LEADER_B", "pumpfun_curve"), (232, "LEADER_A", "meteora_damm_v2"),
            (233, "LEADER_C", "pumpswap_amm")]
    for pid, leader, venue in rows:
        tok = f"TOK{pid}"
        timing = {"fill_model": f"{venue}+0.8s", "filled_at": T0 + pid + 0.8}
        db.execute("INSERT INTO positions(id,chain,token,leader,token_amount,cost,opened_at,paper,status,exit_value,"
                   "closed_at,exit_reason,timing) VALUES(?,'solana',?,?,?,2.0,?,1,'closed',1.6,?,'stop_loss',?)",
                   (pid, tok, leader, str(TOKENS), T0 + pid, T0 + pid + 60, json.dumps(timing)))
        if venue == "pumpswap_amm":        # vault-only ticks of a pool with V = 17.5 SOL: trades move vault and tokens
            Q, V, B = 30 * L, int(17.5 * L), 10**12
            k = (Q + V) * B
            states, slot = [], 100
            for net_in in (int(0.5 * L), int(1.0 * L), int(-0.3 * L), int(2.0 * L), int(-0.8 * L)):
                if net_in > 0:                                   # buy: vault gains net x (1 + lp fee)
                    B = -(-k // (Q + V + net_in)); Q += net_in * 10_020 // 10_000
                else:                                            # sell: gross out g, vault pays g x (1 - lp fee)
                    g = -net_in; B = -(-k // (Q + V - g)); Q -= g * (10_000 - 20) // 10_000
                k = (Q + V) * B
                slot += 1
                states.append((T0 + pid + 5 * len(states), slot, B, Q))
            for ts, sl, b, q in states:
                db.execute("INSERT INTO ticks VALUES(?,?,?,?,?,?,NULL,NULL)", (ts, sl, tok, venue, b, q))
        else:
            for dt in (0, 10, 30):
                db.execute("INSERT INTO ticks VALUES(?,?,?,?,?,?,?,?)", (T0 + pid + dt, 1, tok, venue, 10**12, 10**11, 10**12, 10**11))
        if venue == "meteora_damm_v2":
            db.execute("INSERT INTO pool_factors(ts, token, factor) VALUES(?,?,1.0)", (T0, tok))
    db.commit(); db.close()
    FEES = {"pumpfun_curve": 125, "pumpswap_amm": 120, "meteora_damm_v2": 100}
    ids = lambda d: sorted(p.pid for p in d)
    data, *_ = load(path, 230, FEES, 0.01, 3600)
    assert ids(data) == [230, 231, 232, 233], ids(data)
    data, _, _, _, dropped = load(path, 230, FEES, 0.01, 3600, leaders=("LEADER_A",))
    assert ids(data) == [230, 232] and dropped["leader"] == [231, 233]
    data, _, _, _, dropped = load(path, 230, FEES, 0.01, 3600, exclude_leaders=("LEADER_A", "LEADER_C"))
    assert ids(data) == [231] and dropped["leader"] == [230, 232, 233]
    data, _, _, _, dropped = load(path, 230, FEES, 0.01, 3600, leaders=("LEADER_A", "LEADER_B"), exclude_leaders=("LEADER_B",))
    assert ids(data) == [230, 232]
    data, _, _, _, dropped = load(path, 230, FEES, 0.01, 3600, pool_only=True)
    assert ids(data) == [230, 231, 233] and dropped["pool_out"] == [232]
    data, *_ = load(path, 230, FEES, 0.01, 3600, to_id=231)
    assert ids(data) == [230, 231]                                              # --to-id (H11 halves)
    assert all(p.token == f"TOK{p.pid}" and p.leader.startswith("LEADER_") for p in data)
    print("filters: --leaders / --exclude-leaders (prefixes), --pool-only, token + leader on Pos")

    # PumpSwap: vault-only ticks are re-priced on vault + V (V reconstructed from the trades), fill rescaled
    p = [x for x in data if x.pid == 230][0]
    st = p.series[0][1]
    assert st.real_sol is not None and abs((st.sol_res - st.real_sol) / L - 17.5) < 0.05, (st.sol_res - st.real_sol) / L
    raw, *_ = load(path, 230, FEES, 0.01, 3600, pool_only=True, pumpswap_virtual=False)
    q = [x for x in raw if x.pid == 230][0]
    assert q.series[0][1].real_sol is None and q.series[0][1].sol_res < st.sol_res
    assert p.tokens < q.tokens and abs(p.tokens / q.tokens - 1 / (1 + 17.5 / 30)) < 0.05    # fewer tokens for 2 SOL
    db = Store(path).db                                      # a pool without any trade in its ticks: vault price kept
    db.execute("DELETE FROM ticks WHERE token='TOK233'")
    db.execute("INSERT INTO ticks VALUES(?,?,?,?,?,?,NULL,NULL)", (T0 + 233, 1, "TOK233", "pumpswap_amm", 10**12, 30 * L))
    db.commit(); db.close()
    data, _, _, _, dropped = load(path, 230, FEES, 0.01, 3600, pool_only=True)
    assert ids(data) == [230, 231, 233] and dropped["vault_only"] == [233]
    assert [x for x in data if x.pid == 233][0].series[0][1].real_sol is None
    vs = VirtualSeries([])
    assert vs.at(0) is None and with_virtual([], vs) == []
    print("pumpswap virtual: V reconstructed within 0.05 SOL, fill rescaled, no trade -> vault price kept and listed")


if __name__ == "__main__":
    test_ladder()
    test_arms()
    test_filters()
    print("ALL PASSED")
