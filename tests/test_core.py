"""Offline tests: run with `python -m tests.test_core` from the project root."""
import asyncio, os, tempfile, time
from bot.chains.solana import parse_wallet_swap, WSOL, USDC
from bot.models import Fill, Position
from bot.positions import PositionManager
from bot.risk import RiskManager
from bot.store import Store

W, TOKEN = "Leader111", "Mint222"

def tx(sol_pre, sol_post, tok_pre=None, tok_post=None, mint=TOKEN, err=None, extra=()):
    pre = [] if tok_pre is None else [{"mint": mint, "owner": W, "uiTokenAmount": {"amount": str(tok_pre)}}]
    post = [] if tok_post is None else [{"mint": mint, "owner": W, "uiTokenAmount": {"amount": str(tok_post)}}]
    for m, a, b in extra:
        pre.append({"mint": m, "owner": W, "uiTokenAmount": {"amount": str(a)}})
        post.append({"mint": m, "owner": W, "uiTokenAmount": {"amount": str(b)}})
    return {"meta": {"err": err, "preBalances": [sol_pre, 0], "postBalances": [sol_post, 0],
                     "preTokenBalances": pre, "postTokenBalances": post},
            "transaction": {"message": {"accountKeys": [{"pubkey": W}, {"pubkey": "Other"}]}}}

def test_parser():
    L = 1_000_000_000
    assert parse_wallet_swap(tx(5*L, 4*L, None, 1000), W) == ("buy", TOKEN, 1000, 1.0)
    assert parse_wallet_swap(tx(4*L, 5.5*L, 1000, 0), W) == ("sell", TOKEN, 1000, 1.5)
    # WSOL-quoted buy: native SOL barely moves, WSOL account pays
    assert parse_wallet_swap(tx(5*L, 5*L - 5000, None, 700, extra=[(WSOL, 2*L, L)]), W)[0] == "buy"
    assert parse_wallet_swap(tx(5*L, 4*L, None, 1000, err={"x": 1}), W) is None        # failed tx
    assert parse_wallet_swap(tx(5*L, 5*L - 5000, None, 1000), W) is None               # airdrop/transfer
    assert parse_wallet_swap(tx(5*L, 5*L, None, 1000, extra=[(USDC, 100, 0)]), W) is None  # USDC quoted
    print("parser ok")

class FakeAdapter:
    def __init__(self, path): self.path, self.i, self.sells = path, 0, []
    async def quote_exit(self, p):
        v = self.path[min(self.i, len(self.path) - 1)]; self.i += 1; return v
    async def sell(self, p, slip=None):
        self.sells.append(slip); return Fill(True, "sig", p.token_amount, self.path[min(self.i, len(self.path)-1)])

async def run_path(path, cfg_extra=None, age=0):
    store = Store(tempfile.mktemp())
    cfg = {"take_profit": 0.2, "stop_loss": 0.04, "max_hold_s": 600, **(cfg_extra or {})}
    a = FakeAdapter(path); pm = PositionManager(cfg, {"solana": a}, store)
    p = Position(None, "solana", TOKEN, W, 1000, 1.0, time.time() - age, None, True)
    p.id = store.open_position(p); pm.add(p)
    for _ in range(len(path)):
        if p.status == "closed": break
        await pm._check(p)
    return p, store

async def test_exits():
    p, _ = await run_path([1.05, 1.1, 1.21]); assert p.exit_reason == "take_profit", p.exit_reason
    p, _ = await run_path([0.99, 0.95]);       assert p.exit_reason == "stop_loss"
    p, _ = await run_path([1.0], age=700);     assert p.exit_reason == "max_hold"
    p, s = await run_path([0.99, 0.70])  # gap through the stop: realised loss is -30%, not -4%
    assert p.exit_reason == "stop_loss" and abs(s.realized_pnl_today("solana", True) + 0.30) < 1e-9
    print("exits ok (incl. gap-through-stop realised at -30%)")

def test_risk():
    d = tempfile.mkdtemp(); store = Store(os.path.join(d, "db"))
    cfg = {"max_open_positions": 2, "kill_switch_file": os.path.join(d, "STOP")}
    r = RiskManager(cfg, store, True); cc = {"daily_loss_limit": 1.0}
    assert r.can_open("solana", "A", [], set(), cc)[0]
    pos = [Position(1, "solana", "A", W, 1, 1, 0, None, True)]
    assert r.can_open("solana", "A", pos, set(), cc)[1] == "already in this token"
    assert r.can_open("solana", "B", pos, {("solana", "C")}, cc)[1] == "max open positions"
    lost = Position(None, "solana", "Z", W, 1, 2.0, time.time(), None, True)
    lost.id = store.open_position(lost); lost.status, lost.exit_value, lost.closed_at = "closed", 0.5, time.time()
    store.close_position(lost)
    assert r.can_open("solana", "B", [], set(), cc)[1] == "daily loss limit hit"
    # one_entry_per_token: Z was held (closed) -> skipped, also after a restart (new Store/RiskManager on the same db)
    cc1 = {"one_entry_per_token": True}
    r.cfg["reentry_cooldown_s"] = 0
    assert r.can_open("solana", "Z", [], set(), {})[0]                  # option off (default): no block
    r2 = RiskManager(dict(cfg, reentry_cooldown_s=0), Store(os.path.join(d, "db")), True)
    assert r2.can_open("solana", "Z", [], set(), cc1)[1] == "already traded this token"
    assert r2.can_open("solana", "Y", [], set(), cc1)[0]                # never held
    assert RiskManager(cfg, Store(os.path.join(d, "db")), False).can_open("solana", "Z", [], set(), cc1)[0]  # paper row != live
    prof = Position(None, "solana", "P", W, 1, 1.0, time.time(), None, True, timing={"profile": "x"})
    store.open_position(prof)
    assert r2.can_open("solana", "P", [], set(), cc1)[0]                # profile positions don't count
    open(cfg["kill_switch_file"], "w").close()
    assert r.can_open("solana", "B", [], set(), cc)[1] == "kill switch file present"
    print("risk ok")

if __name__ == "__main__":
    test_parser(); asyncio.run(test_exits()); test_risk(); print("ALL PASSED")
