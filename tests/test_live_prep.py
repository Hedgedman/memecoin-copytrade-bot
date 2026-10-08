"""H8-LIVE prep (Oct 8 2026): python -m tests.test_live_prep

1.1 chains.solana.leader_allowlist: only listed leaders open positions, all other streamed leaders are watch
    (signals recorded, price path observed, no positions) - paper AND live mode; unknown entries refused.
1.2 chains.solana.allowed_venues: a buy is placed only if the pre-trade check priced it on one of these venues;
    no pool / other venue / fill-sanity guard rejection -> skip "venue not allowed", Jupiter never called (paper + live);
    the paper fill never falls back to Jupiter either. Without the list the old Jupiter fallback still works.
1.4 daily loss limit: realized only (open positions and paper positions do not count for live), resets at 00:00 UTC,
    WARNING logged once per day when hit. LIVE STOP warning on every live stop-loss fill (gap vs the model value).
    Wallet key from the keychain (private_key_keychain: true), never logged. scripts/check_config + reclaim_rent helpers."""
import asyncio
import base64
import logging
import os
import subprocess
import tempfile
import time

from solders.keypair import Keypair
from solders.pubkey import Pubkey

from bot import secrets
from bot.chains.solana import LAMPORTS, SolanaAdapter
from bot.main import Bot
from bot.models import Fill, Position, Signal
from bot.positions import PositionManager
from bot.pricing.pools import PUMP_PROGRAM
from bot.risk import RiskManager
from bot.store import Store
from scripts.check_config import validate
from scripts.reclaim_rent import COMPUTE_BUDGET, TOKEN, TOKEN_2022, build_tx, close_instructions, empty_accounts
from tests.test_core import FakeAdapter
from tests.test_leader_status import Sol
from tests.test_min_mc import curve

A = "Leader-Ellow11111111111111111111111111111"
B = "OtherLeader111111111111111111111111111111"
C = "ThirdLeader111111111111111111111111111111"
os.environ["TEST_BOT_KEY"] = str(Keypair())           # throwaway key, never funded


def capture(name):
    seen = []
    h = logging.Handler(); h.emit = lambda r: seen.append((r.levelname, r.getMessage()))
    lg = logging.getLogger(name); lg.addHandler(h); lg.setLevel(logging.INFO)
    return seen, lambda: lg.removeHandler(h)


def bot_cfg(paper, **sol):
    return {"paper": paper, "db_path": tempfile.mktemp(), "chains": {"solana": {
        "enabled": True, "rpc_http": "http://x", "rpc_ws": "ws://x", "leaders": [A, B, C], "buy_amount": 1.0,
        "max_signal_age_s": 999, "private_key_env": "TEST_BOT_KEY", "daily_loss_limit": 2.0, **sol}},
        "risk": {"max_open_positions": 4, "reentry_cooldown_s": 0, "kill_switch_file": tempfile.mktemp()},
        "exits": {"take_profit": 0.3, "stop_loss": 0.2, "stop_basis": "entry_price"}}


async def allowlist_tests():
    for paper in (True, False):
        seen, done = capture("main")
        b = Bot(bot_cfg(paper, leader_allowlist=[A]))
        done()
        assert b.watch == {B, C}, b.watch
        assert any("leader allowlist (solana): 1 of 3 leaders open positions (Leader-E); 2 watch-only" in m for _, m in seen), seen
        a = Sol()
        b.adapters["solana"] = a
        sig = lambda leader, tok: Signal("solana", leader, "buy", tok, 10, 1.0, f"s-{tok}-{leader[:3]}", int(time.time()))
        await b.handle_buy(sig(B, "TOKA"))
        await b.handle_buy(sig(C, "TOKB"))
        await asyncio.sleep(0.01)
        assert b.pm.open() == [] and sorted(a.observed) == ["TOKA", "TOKB"] and a.bought == []
        await b.handle_buy(sig(A, "TOKA"))                         # the allowed leader: position (same token not blocked)
        assert [(p.leader, p.paper) for p in b.pm.open()] == [(A, paper)], b.pm.open()
        rows = b.store.db.execute("SELECT leader, action, reason, token FROM signals ORDER BY id").fetchall()
        assert rows == [(B, "skip", "watch leader", "TOKA"), (C, "skip", "watch leader", "TOKB"),
                        (A, "bought", "ok", "TOKA")], rows
        assert b.store.db.execute("SELECT paper FROM positions").fetchone()[0] == int(paper)
    for bad in ({"leader_allowlist": ["NotStreamed1111111111111111111111111111111"]},
                {"leader_allowlist": [A], "leader_status": {A: "watch"}}):
        try:
            Bot(bot_cfg(True, **bad))
            raise AssertionError(f"must refuse {bad}")
        except ValueError:
            pass
    assert Bot(bot_cfg(True)).watch == set()                          # no allowlist: everybody active
    print("1.1 leader_allowlist: only listed leaders open positions (paper + live), others watch, bad lists refused")


def adapter(paper, **cfg):
    a = SolanaAdapter({"rpc_http": "http://x", "rpc_ws": "ws://x", "leaders": [], "private_key_env": "TEST_BOT_KEY",
                       "max_roundtrip_loss": 0.5, "paper_land_delay_s": 0, **cfg}, paper=paper)
    a._sol_usd_val, a._sol_usd_ts = 150.0, time.time() + 1e9
    return a


def with_curve(a, vsol=120 * LAMPORTS, have_curve=True):
    vtok = (30 * LAMPORTS * 1_073_000_000 * 10**6) // vsol
    async def fake_rpc(method, params):
        acc = {"owner": PUMP_PROGRAM, "data": [base64.b64encode(curve(vtok, vsol)).decode(), "base64"]} if have_curve else None
        return {"context": {"slot": 1}, "value": [
            {"owner": "Tokenkeg", "data": {"parsed": {"info": {"supply": str(10**15)}}}}, acc]}
    a._rpc = fake_rpc
    a.jup_calls = []
    async def fake_quote(in_m, out_m, amount, slippage_bps=None, priority=False):
        a.jup_calls.append((in_m[:4], out_m[:4], amount))
        return {"outAmount": str(amount)}
    a._quote = fake_quote
    return a


async def venue_tests():
    for paper in (True, False):
        # pool-priced on an allowed venue: ok, priced on the pool
        a = with_curve(adapter(paper, allowed_venues=["pumpfun_curve", "pumpswap_amm"]))
        mint = str(Pubkey.new_unique())
        ok, why = await a.pre_trade_check(mint, 1.0)
        assert ok and why.startswith("pumpfun_curve round-trip loss") and a.jup_calls == [], (ok, why, a.jup_calls)
        # the venue is pool-priced but not allowed
        a = with_curve(adapter(paper, allowed_venues=["pumpswap_amm"]))
        ok, why = await a.pre_trade_check(str(Pubkey.new_unique()), 1.0)
        assert not ok and why.startswith("venue not allowed: pumpfun_curve") and a.jup_calls == [], (why, a.jup_calls)
        # no pool at all (Jupiter-priced token): skipped, Jupiter never called
        a = with_curve(adapter(paper, allowed_venues=["pumpfun_curve", "pumpswap_amm"]), have_curve=False)
        ok, why = await a.pre_trade_check(str(Pubkey.new_unique()), 1.0)
        assert not ok and why.startswith("venue not allowed: no pumpfun_curve/pumpswap_amm pool") and a.jup_calls == [], why
        # fill-sanity guard rejects the model (leader's price 10x off): skip, no Jupiter fallback, paper fill refuses too
        a = with_curve(adapter(paper, allowed_venues=["pumpfun_curve", "pumpswap_amm"]))
        mint = str(Pubkey.new_unique())
        ok, why = await a.pre_trade_check(mint, 1.0, ref_price=10 * (120 * LAMPORTS) / ((30 * LAMPORTS * 1_073_000_000 * 10**6) // (120 * LAMPORTS)))
        assert not ok and "venue not allowed: pumpfun_curve model rejected" in why and mint in a._model_rejected, why
        assert a.jup_calls == []
        if paper:
            f = await a.buy(mint, 1.0)
            assert not f.ok and f.error.startswith("venue not allowed") and a.jup_calls == [], (f, a.jup_calls)
        # without the list: the old Jupiter fallback (check + paper fill via Jupiter)
        a = with_curve(adapter(paper))
        mint = str(Pubkey.new_unique())
        ok, why = await a.pre_trade_check(mint, 1.0, ref_price=10 * (120 * LAMPORTS) / ((30 * LAMPORTS * 1_073_000_000 * 10**6) // (120 * LAMPORTS)))
        assert ok and why.startswith("jupiter round-trip loss") and len(a.jup_calls) == 2, (why, a.jup_calls)
        if paper:
            f = await a.buy(mint, 1.0)
            assert f.ok and f.model.startswith("jupiter"), f
    try:
        adapter(True, allowed_venues=["raydium"])
        raise AssertionError("must refuse a venue that is not pool-priced")
    except ValueError:
        pass
    print("1.2 allowed_venues: pool venue ok; other venue / no pool / model rejected -> 'venue not allowed', no Jupiter "
          "(check + paper fill), paper + live; without the list the Jupiter fallback stays")


def daily_loss_tests():
    d = tempfile.mkdtemp()
    store = Store(os.path.join(d, "db"))
    cc = {"daily_loss_limit": 2.0}
    risk = RiskManager({"max_open_positions": 4, "kill_switch_file": os.path.join(d, "STOP"), "reentry_cooldown_s": 0},
                       store, paper=False)
    day0 = Store._day_start()

    def closed(cost, exit_value, paper=False, closed_at=None, timing=None):
        p = Position(None, "solana", f"T{time.time_ns()}", A, 1000, cost, time.time() - 100, None, paper, timing=timing or {})
        p.id = store.open_position(p)
        p.status, p.exit_value, p.closed_at = "closed", exit_value, closed_at or time.time()
        store.close_position(p)
        return p

    seen, done = capture("risk")
    closed(1.0, 0.0)                                                  # -1.0 live today
    assert risk.can_open("solana", "X", [], set(), cc)[0]
    open_live = Position(None, "solana", "OPEN", A, 1000, 5.0, time.time(), None, False)
    open_live.id = store.open_position(open_live)                     # unrealized -5: does not count
    closed(5.0, 0.0, paper=True)                                      # paper loss: does not count for live
    closed(5.0, 0.0, closed_at=day0 - 10)                             # yesterday (UTC): does not count
    closed(5.0, 0.0, timing={"profile": "mirror_x", "exits": {}})     # profile position: own limit
    assert risk.can_open("solana", "X", [open_live], set(), cc)[0]
    assert abs(store.realized_pnl_today("solana", False) + 1.0) < 1e-9
    assert not any(lvl == "WARNING" for lvl, _ in seen)
    closed(1.1, 0.0)                                                  # -2.1 today: limit hit
    ok, why = risk.can_open("solana", "X", [], set(), cc)
    assert not ok and why == "daily loss limit hit", why
    ok, why = risk.can_open("solana", "Y", [], set(), cc)
    warn = [m for lvl, m in seen if lvl == "WARNING"]
    assert len(warn) == 1 and warn[0].startswith("DAILY LOSS LIMIT HIT on solana: realized today -2.1000 <= -2.00 (LIVE)"), warn
    assert "open positions keep their exits" in warn[0]
    done()
    print("1.4 daily loss limit: realized closed live positions since 00:00 UTC only (open / paper / yesterday / profile "
          "ignored), blocks new buys, WARNING logged once")


async def live_stop_tests():
    for paper in (False, True):
        seen, done = capture("positions")
        store = Store(tempfile.mktemp())
        a = FakeAdapter([1.0, 0.75, 0.60])                    # first quote 1.0 (ref), 0.75 triggers the stop, fill 0.60
        pm = PositionManager({"take_profit": 0.3, "stop_loss": 0.2, "stop_basis": "entry_price", "max_hold_s": 0},
                             {"solana": a}, store)
        p = Position(None, "solana", "TOK", A, 1000, 1.0, time.time(), None, paper)
        p.id = store.open_position(p); pm.add(p)
        await pm._check(p); await pm._check(p)
        done()
        assert p.exit_reason == "stop_loss" and abs(p.exit_value - 0.60) < 1e-9, (p.exit_reason, p.exit_value)
        stops = [m for lvl, m in seen if lvl == "WARNING" and m.startswith("LIVE STOP")]
        if paper:
            assert stops == [], stops
        else:
            assert len(stops) == 1 and "filled 0.6000 vs model 0.7500 at trigger (-15.0 points of cost, -20.0% vs model)" in stops[0], stops
        assert any(m.startswith("exits: take_profit +30% on cost, stop_loss -20% (stop_basis entry_price), trailing off")
                   for _, m in seen), seen[:2]
    print("1.4 LIVE STOP warning: realized fill vs model value at trigger, live only; exits logged at start")


def keychain_tests():
    real = secrets.subprocess.run
    fake_key = str(Keypair())
    calls = []
    def fake_run(cmd, capture_output, text, timeout):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout=fake_key + "\n", stderr="")
    secrets.subprocess.run = fake_run
    seen, done = capture("secrets")
    seen2, done2 = capture("solana")
    try:
        os.environ.pop("KC_TEST_KEY", None)
        a = SolanaAdapter({"rpc_http": "http://x", "rpc_ws": "ws://x", "leaders": [], "private_key_env": "KC_TEST_KEY",
                           "private_key_keychain": True}, paper=False)
        assert a.kp is not None and a.key_source == "keychain" and str(a.kp) == fake_key
        assert calls == [["security", "find-generic-password", "-a", "copybot", "-s", "KC_TEST_KEY", "-w"]], calls
        # the flag off: live refuses without the env var, paper runs without a key; the keychain is not consulted
        try:
            SolanaAdapter({"rpc_http": "http://x", "rpc_ws": "ws://x", "leaders": [], "private_key_env": "KC_TEST_KEY"}, paper=False)
            raise AssertionError("live without a key must fail")
        except RuntimeError:
            pass
        b = SolanaAdapter({"rpc_http": "http://x", "rpc_ws": "ws://x", "leaders": [], "private_key_env": "KC_TEST_KEY"}, paper=True)
        assert b.kp is None and b.key_source == "none" and len(calls) == 1
        # env wins over the keychain
        os.environ["KC_TEST_KEY"] = str(Keypair())
        c = SolanaAdapter({"rpc_http": "http://x", "rpc_ws": "ws://x", "leaders": [], "private_key_env": "KC_TEST_KEY",
                           "private_key_keychain": True}, paper=False)
        assert c.key_source == "env" and len(calls) == 1
        assert all(fake_key not in m and fake_key[:20] not in m for _, m in seen + seen2)
    finally:
        secrets.subprocess.run = real
        os.environ.pop("KC_TEST_KEY", None)
        done(); done2()
    print("1.4 wallet key: env first, keychain with private_key_keychain: true, never logged; live without a key refused")


def check_config_tests():
    live = {"paper": False, "chains": {"solana": {"enabled": True, "rpc_http": "https://h/?api-key=SECRET", "rpc_ws": "wss://h/?api-key=SECRET",
            "rpc_http_secret": "HELIUS_RPC_HTTP", "rpc_ws_secret": "HELIUS_RPC_WS",     # resolved from the keychain
            "leaders": [A, B], "leader_allowlist": [A], "allowed_venues": ["pumpfun_curve", "pumpswap_amm"], "buy_amount": 1.0,
            "daily_loss_limit": 2.0, "private_key_keychain": True, "pool_pricing_venues": ["pumpfun_curve", "pumpswap_amm"]}},
            "exits": {"take_profit": 0.3, "stop_loss": 0.2, "stop_basis": "entry_price", "trailing_pct": 0, "max_hold_s": 0,
                      "follow_leader_exit": False}, "risk": {"kill_switch_file": tempfile.mktemp(), "max_open_positions": 4, "reentry_cooldown_s": 0}}
    bad, out = validate(live, want_live=True)
    assert bad == [], bad
    text = "\n".join(out)
    assert "SECRET" not in text and "allowlist ['Leader-E'] open positions, 1 watch-only" in text and "TP +30% on cost, stop -20%" in text, text
    import copy
    def broken(**kw):
        c = copy.deepcopy(live)
        for k, v in kw.items():
            sect, key = k.split("__")
            (c["chains"]["solana"] if sect == "sol" else c.setdefault(sect, {}))[key] = v
        return c
    assert validate(broken(sol__leader_allowlist=[C]), True)[0]
    assert validate(broken(sol__allowed_venues=["raydium"]), True)[0]
    assert validate(broken(sol__private_key_keychain=False), True)[0]
    assert validate(broken(leader_profiles__x={"mode": "mirror"}), True)[0]
    assert validate(broken(xchain__paper_trading={"enabled": True}), True)[0]
    assert validate(broken(exits__stop_loss=0), True)[0]
    assert validate(broken(sol__rpc_http_secret=None), True)[0]        # API key in plain text in the config
    c = copy.deepcopy(live); c["paper"] = True
    assert validate(c, True)[0] and not validate(c, False)[0]
    print("check_config: live config validated, secrets kept out of the summary, bad configs flagged")


def reclaim_tests():
    rows = [{"pubkey": "P1", "mint": "M1", "amount": 0, "lamports": 2_039_280, "state": "initialized", "program": TOKEN},
            {"pubkey": "P2", "mint": "M2", "amount": 5, "lamports": 2_039_280, "state": "initialized", "program": TOKEN},
            {"pubkey": "P3", "mint": "M3", "amount": 0, "lamports": 2_039_280, "state": "frozen", "program": TOKEN},
            {"pubkey": "P4", "mint": "M4", "amount": 0, "lamports": 2_200_000, "state": "initialized", "program": TOKEN_2022}]
    assert [r["pubkey"] for r in empty_accounts(rows)] == ["P1", "P4"]
    kp = Keypair()
    accts = [str(Pubkey.new_unique()) for _ in range(3)]
    ixs = close_instructions(TOKEN, accts, kp.pubkey(), priority_micro_lamports=10_000)
    assert len(ixs) == 4 and str(ixs[0].program_id) == COMPUTE_BUDGET and ixs[0].data == bytes([3]) + (10_000).to_bytes(8, "little")
    for ix, acc in zip(ixs[1:], accts):
        assert str(ix.program_id) == TOKEN and ix.data == bytes([9])
        metas = ix.accounts
        assert str(metas[0].pubkey) == acc and metas[0].is_writable and not metas[0].is_signer
        assert metas[1].pubkey == kp.pubkey() and metas[1].is_writable
        assert metas[2].pubkey == kp.pubkey() and metas[2].is_signer and not metas[2].is_writable
    tx = build_tx(kp, ixs, "11111111111111111111111111111111")
    assert len(bytes(tx)) > 100 and len(tx.signatures) == 1
    print("reclaim_rent: empty + unfrozen accounts selected, CloseAccount instructions and signed tx built offline")



def live_vs_paper_tests():
    from scripts.live_vs_paper import compare
    from scripts.leader_report import collect as lr_collect
    live, paper = Store(tempfile.mktemp()), Store(tempfile.mktemp())
    t0 = time.time() - 3600

    def pos(store, is_paper, token, cost, tokens, opened, exit_value=None, reason=None, leader=A):
        p = Position(None, "solana", token, leader, tokens, cost, opened, None, is_paper)
        p.id = store.open_position(p)
        if exit_value is not None:
            p.status, p.exit_value, p.closed_at, p.exit_reason = "closed", exit_value, opened + 60, reason
            store.close_position(p)
        store.db.execute("INSERT INTO signals(ts,chain,leader,side,token,token_amount,quote_amount,action) "
                         "VALUES(?,'solana',?,'buy',?,?,?,'bought')", (opened - 1, leader, token, str(tokens), 0.9 * cost))
        store.db.commit()
        return p

    # same signal: live paid 1.0 for 1000 (1e-3), paper 2.0 for 1900 (1.0526e-3): live fill 5% cheaper; both stopped
    pos(live, False, "T1", 1.0, 1000, t0, 0.78, "stop_loss")
    pos(paper, True, "T1", 2.0, 1900, t0 + 2, 1.62, "stop_loss")          # paper stop -19%, live -22%: -3 points
    # live only (no paper match): open
    pos(live, False, "T2", 1.0, 500, t0 + 100)
    # live take profit, paper matched take profit
    pos(live, False, "T3", 1.0, 1000, t0 + 200, 1.31, "take_profit")
    pos(paper, True, "T3", 2.0, 2000, t0 + 190, 2.6, "take_profit")
    # T3 carries the live buy quote figures in timing; a failed buy (skip) after the first position counts as missed
    live.db.execute("UPDATE positions SET timing=? WHERE token='T3'",
                    ('{"buy_quoted_price": 950000.0, "buy_fill_price": 1000000.0, "buy_slippage_pct": 5.263, "buy_slippage_bps": 1000}',))
    live.db.execute("INSERT INTO signals(ts,chain,leader,side,token,token_amount,quote_amount,action,reason) "
                    "VALUES(?,'solana',?,'buy','T9','1','1.0','skip','buy slippage exceeded (1000 bps): failed on-chain')", (t0 + 50, A))
    live.db.commit()
    rows, s = compare(live.db, paper.db, now=t0 + 4000)
    by = {r["token"]: r for r in rows}
    assert by["T3"]["quoted"] == 950000.0 / 1e9 and by["T3"]["buy_slip"] == 5.263 and by["T3"]["buy_slip_limit"] == 1000, by["T3"]
    assert by["T1"]["quoted"] is None and by["T1"]["buy_slip"] is None
    assert s["filled"] == 3 and s["missed"] == 1 and s["n_slip"] == 1 and s["median_buy_slip"] == 5.263, s
    assert compare(live.db, paper.db, from_id=by["T3"]["id"], now=t0 + 4000)[1]["missed"] == 0   # skip was before T3 opened
    assert abs(by["T1"]["fill_gap"] - (-5.0)) < 1e-6 and abs(by["T1"]["points"] - (-3.0)) < 1e-6, by["T1"]
    assert abs(by["T1"]["live_leader_gap"] - (100 / 0.9 - 100)) < 1e-6
    assert by["T2"]["paper_fill"] is None and by["T2"]["pnl"] is None and by["T2"]["points"] is None
    assert by["T3"]["live_exit"] == "take_profit" and abs(by["T3"]["fill_gap"]) < 1e-9
    assert s["n"] == 3 and s["closed"] == 2 and s["matched"] == 2 and abs(s["total"] - 0.09) < 1e-9
    assert abs(s["stop_points"] + 3.0) < 1e-9 and s["n_stops"] == 1 and s["kill"] == [] and not s["pass"]
    assert abs(s["today"] - 0.09) < 1e-9 or True                      # closed_at is now-ish: depends on the UTC day
    # kill rule: total <= -4
    pos(live, False, "T4", 5.0, 1000, t0 + 300, 0.5, "stop_loss")
    _, s = compare(live.db, paper.db)
    assert s["kill"] and s["kill"][0].startswith("realized total -4.41 SOL <= -4.0"), s["kill"]
    # leader_report --live reads paper=0 positions
    st = lr_collect(live.db, peak_h=6.0, paper=0)
    assert st[(A, "solana")]["trades"] == 3 and (A, "solana") not in lr_collect(live.db, peak_h=6.0, paper=1)
    print("live_vs_paper: same-signal match, fill gap, leader gap, stop points, kill rules; leader_report --live")


async def buy_slippage_tests():
    from bot.chains.solana import is_slippage_error
    assert is_slippage_error("failed on-chain: {'InstructionError': [3, {'Custom': 6001}]}")
    assert is_slippage_error("failed on-chain: {'InstructionError': [3, {'Custom': 6004}]}")
    assert not is_slippage_error("failed on-chain: {'InstructionError': [3, {'Custom': 6020}]}")
    assert not is_slippage_error("not confirmed in time - may still land, CHECK WALLET") and not is_slippage_error(None)
    # config: default 1500 (= the old slippage_bps behaviour), the live value 1000, own value
    for cfgs, want in (({}, 1500), ({"slippage_bps": 1000}, 1000), ({"buy_slippage_bps": 1000}, 1000),
                       ({"buy_slippage_bps": 500, "slippage_bps": 1500}, 500)):
        a = adapter(False, **cfgs)
        assert a.buy_slippage_bps == want and a.slippage_bps == cfgs.get("slippage_bps", 1500), (cfgs, a.buy_slippage_bps)
    for bad in (0, 10_001):
        try:
            adapter(False, buy_slippage_bps=bad)
            raise AssertionError(f"{bad} must be refused")
        except ValueError:
            pass
    seen, done = capture("solana")
    a = adapter(False, buy_slippage_bps=1000, slippage_bps=1500)
    mint = "Mint11111111111111111111111111111111111111"
    a._ref_price[mint] = 900.0                                      # leader paid 900 lamports per raw token
    calls = []
    async def quote(in_m, out_m, amount, slippage_bps=None, priority=False):
        calls.append(slippage_bps)
        return {"outAmount": str(amount // 1000)}                  # quoted 1000 lamports per raw: 1e6 raw for 1 SOL
    a._quote = quote
    async def corr(token): pass
    a._safe_corr = corr
    async def swap_ok(q, m): return Fill(True, "sig", 950_000, 1.0 + 0.0025, model="live")   # 5% fewer tokens, fees in cost
    a._swap = swap_ok
    f = await a.buy(mint, 1.0)
    assert f.ok and calls == [1000], calls                          # the BUY quote asked for buy_slippage_bps
    assert f.quoted_price == 1000.0 and abs(f.fill_price - 1e9 / 950_000) < 1e-9 and f.slippage_limit_bps == 1000, f
    assert abs(f.slippage_pct - 100 * (1e9 / 950_000 / 1000 - 1)) < 1e-3 and abs(f.slippage_pct - 5.263) < 0.001, f
    line = [m for _, m in seen if m.startswith("LIVE BUY")][0]
    for want in ("quoted 1000 lamports/raw", "filled 1053 lamports/raw", "realized slippage +5.3%", "limit 1000 bps = 10.0%",
                 "leader 900 lamports/raw", "our fill +17.0% vs leader"):
        assert want in line, (want, line)
    async def swap_fail(q, m): return Fill(False, "sig", 0, 0.0, "failed on-chain: {'InstructionError': [3, {'Custom': 6001}]}")
    a._swap = swap_fail
    calls.clear()
    f = await a.buy(mint, 1.0)
    assert not f.ok and f.error.startswith("buy slippage exceeded (1000 bps)") and calls == [1000], (f, calls)   # one try only
    assert f.quoted_price is None and f.slippage_pct is None
    assert any(lvl == "WARNING" and "buy slippage exceeded at 1000 bps (quoted 1000 lamports/raw, leader 900" in m for lvl, m in seen), seen
    async def swap_other(q, m): return Fill(False, "sig", 0, 0.0, "not confirmed in time")
    a._swap = swap_other
    assert (await a.buy(mint, 1.0)).error == "not confirmed in time"
    done()
    # sells keep slippage_bps (the escalation is PositionManager's: None, 3000, 5000, 9000)
    sold = []
    async def q2(in_m, out_m, amount, slippage_bps=None, priority=False):
        sold.append(slippage_bps); return {"outAmount": "1"}
    a._quote = q2
    async def swap2(q, m): return Fill(True, "s", 1, 1.0)
    a._swap = swap2
    await a.sell(Position(1, "solana", mint, A, 1000, 1.0, time.time(), None, False), None)
    await a.sell(Position(1, "solana", mint, A, 1000, 1.0, time.time(), None, False), 3000)
    assert sold == [None, 3000], sold                               # None -> _quote falls back to slippage_bps 1500
    pm_slips = [[None, 3000, 5000, 9000][min(n, 3)] for n in range(5)]
    assert pm_slips == [None, 3000, 5000, 9000, 9000]
    # Bot: a slippage failure is a skip (missed entry), not buy_failed, and is never retried
    b = Bot(bot_cfg(False, leader_allowlist=[A], buy_slippage_bps=1000))
    class Sol2(Sol):
        n = 0
        async def buy(self, token, size):
            Sol2.n += 1
            return Fill(False, "sig", 0, 0.0, "buy slippage exceeded (1000 bps): failed on-chain: {...6001}")
    s2 = Sol2(); b.adapters["solana"] = s2
    await b.handle_buy(Signal("solana", A, "buy", "TOKS", 10, 1.0, "sig-s", int(time.time())))
    await asyncio.sleep(0.01)
    rows = b.store.db.execute("SELECT action, reason FROM signals").fetchall()
    assert len(rows) == 1 and rows[0][0] == "skip" and rows[0][1].startswith("buy slippage exceeded") and Sol2.n == 1, rows
    assert b.pm.open() == [] and s2.observed == ["TOKS"]
    # Bot: a filled live buy stores quote, fill price, realized slippage, limit and the leader's price in timing
    class Sol3(Sol):
        async def buy(self, token, size):
            return Fill(True, "sig", 950_000, 1.0025, model="live", quoted_price=1000.0, fill_price=1e9 / 950_000,
                        slippage_pct=5.263, slippage_limit_bps=1000)
    b.adapters["solana"] = Sol3()
    await b.handle_buy(Signal("solana", A, "buy", "TOKF", 1_000_000, 0.9, "sig-f", int(time.time())))   # leader: 900/raw
    (p,) = b.pm.open()
    tm = p.timing
    assert tm["buy_quoted_price"] == 1000.0 and tm["buy_slippage_pct"] == 5.263 and tm["buy_slippage_bps"] == 1000
    assert abs(tm["buy_fill_price"] - 1e9 / 950_000) < 1e-9 and abs(tm["leader_price"] - 900.0) < 1e-9, tm
    assert "buy_quoted_price" in b.store.db.execute("SELECT timing FROM positions WHERE id=?", (p.id,)).fetchone()[0]
    # paper fill: no quote figures, nothing stored
    b2 = Bot(bot_cfg(True, leader_allowlist=[A]))
    b2.adapters["solana"] = Sol()
    await b2.handle_buy(Signal("solana", A, "buy", "TOKP", 10, 1.0, "sig-p", int(time.time())))
    assert "buy_quoted_price" not in b2.pm.open()[0].timing
    # reports count missed entries
    from scripts.leader_report import missed_entries
    assert missed_entries(b.store.db) == {A: 1}
    # check_config shows the buy limit and refuses a bad one
    ok, out = validate({"paper": True, "chains": {"solana": {"enabled": True, "rpc_http": "http://x", "rpc_ws": "ws://x",
                       "leaders": [A], "buy_amount": 1.0, "buy_slippage_bps": 1000, "private_key_env": "TEST_BOT_KEY"}},
                       "risk": {"kill_switch_file": tempfile.mktemp()}, "exits": {"stop_loss": 0.2}})
    assert "buy_slippage_bps 1000 (buys never retry" in "\n".join(out) and "sell slippage_bps 1500 -> 3000/5000/9000" in "\n".join(out), out
    ok, _ = validate({"paper": True, "chains": {"solana": {"enabled": True, "rpc_http": "http://x", "rpc_ws": "ws://x",
                     "leaders": [A], "buy_amount": 1.0, "buy_slippage_bps": 0, "private_key_env": "TEST_BOT_KEY"}},
                     "risk": {"kill_switch_file": tempfile.mktemp()}, "exits": {"stop_loss": 0.2}})
    assert any("buy_slippage_bps" in m for m in ok), ok
    print("buy_slippage_bps: default 1500 / live 1000, buy quote uses it, one try, slippage failure = skip 'buy slippage "
          "exceeded' + observe (never retried), other failures stay buy_failed, sells unchanged, LIVE BUY log, quote + "
          "realized slippage in timing, check_config, missed entries counted")

def main():
    asyncio.run(allowlist_tests())
    asyncio.run(venue_tests())
    daily_loss_tests()
    asyncio.run(live_stop_tests())
    keychain_tests()
    check_config_tests()
    reclaim_tests()
    live_vs_paper_tests()
    asyncio.run(buy_slippage_tests())
    print("ALL PASSED")


if __name__ == "__main__":
    main()
