from __future__ import annotations
import json
import sqlite3
import time
from datetime import datetime, timezone
from .models import Position, Signal


class Store:
    """SQLite log of every signal and decision. This is your real research data."""

    def __init__(self, path: str):
        self.db = sqlite3.connect(path)
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS signals(
            id INTEGER PRIMARY KEY, ts REAL, chain TEXT, leader TEXT, side TEXT, token TEXT,
            token_amount TEXT, quote_amount REAL, signature TEXT, block_time INTEGER,
            action TEXT, reason TEXT);
        CREATE TABLE IF NOT EXISTS positions(
            id INTEGER PRIMARY KEY, chain TEXT, token TEXT, leader TEXT, token_amount TEXT,
            cost REAL, opened_at REAL, entry_sig TEXT, paper INTEGER, status TEXT,
            exit_value REAL, closed_at REAL, exit_reason TEXT, exit_sig TEXT);
        CREATE TABLE IF NOT EXISTS marks(
            position_id INTEGER, ts REAL, value REAL);
        CREATE TABLE IF NOT EXISTS ticks(
            ts REAL, slot INTEGER, token TEXT, venue TEXT,
            tok_res INTEGER, sol_res INTEGER, real_tok INTEGER, real_sol INTEGER);
        CREATE INDEX IF NOT EXISTS ticks_token_ts ON ticks(token, ts);
        CREATE TABLE IF NOT EXISTS pool_factors(ts REAL, token TEXT, factor REAL);
        CREATE TABLE IF NOT EXISTS leader_events(ts REAL, leader TEXT, signature TEXT, kind TEXT,
            token TEXT, sol REAL, detail TEXT);
        CREATE INDEX IF NOT EXISTS leader_events_leader ON leader_events(leader, ts);
        CREATE INDEX IF NOT EXISTS pool_factors_token ON pool_factors(token, ts);
        CREATE TABLE IF NOT EXISTS xchain_trades(
            request_id TEXT PRIMARY KEY, ts REAL, leader TEXT, evm_address TEXT, side TEXT,
            chain TEXT, chain_id INTEGER, token TEXT, symbol TEXT, decimals INTEGER,
            token_amount TEXT, usd REAL, his_price_usd REAL, status TEXT,
            deposit_tx TEXT, deposit_ts REAL, fill_tx TEXT, fill_ts REAL, detected_via TEXT,
            price_later_ts REAL, price_later_usd REAL, price_later_impact_pct REAL,
            price_later_out_usd REAL, raw TEXT);
        CREATE INDEX IF NOT EXISTS xchain_trades_leader ON xchain_trades(leader, ts);
        CREATE TABLE IF NOT EXISTS xchain_fills(
            id INTEGER PRIMARY KEY, ts REAL, position_id INTEGER, chain TEXT, token TEXT, symbol TEXT,
            side TEXT, reason TEXT, fraction REAL, his_ref TEXT, his_ts REAL, his_usd REAL,
            his_price_usd REAL, our_usd REAL, our_tokens TEXT, our_price_usd REAL, gap_pct REAL,
            delay_s REAL, impact_pct REAL, gas_usd REAL);
        CREATE INDEX IF NOT EXISTS xchain_fills_pos ON xchain_fills(position_id, ts);
        CREATE TABLE IF NOT EXISTS xchain_addresses(
            leader TEXT, evm_address TEXT, first_seen REAL, last_seen REAL, source TEXT,
            PRIMARY KEY(leader, evm_address));
        CREATE TABLE IF NOT EXISTS xchain_prices(
            request_id TEXT, offset_s REAL, ts REAL, delay_s REAL, price_usd REAL, gap_pct REAL,
            impact_pct REAL, out_usd REAL);
        CREATE INDEX IF NOT EXISTS xchain_prices_req ON xchain_prices(request_id);
        """)
        self.db.commit()
        for table, col, typ in (("signals", "venues", "TEXT"), ("positions", "ref_value", "REAL"),
                               ("positions", "timing", "TEXT"), ("xchain_trades", "detected_at", "REAL"),
                               ("xchain_trades", "detect_delay_s", "REAL"), ("xchain_fills", "leader", "TEXT"),
                               ("xchain_fills", "detect_delay_s", "REAL")):
            try:
                self.db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
            except sqlite3.OperationalError:
                pass   # column already exists
        self.db.commit()

    def log_signal(self, s: Signal, action: str, reason: str = ""):
        self.db.execute("INSERT INTO signals(ts,chain,leader,side,token,token_amount,quote_amount,"
                        "signature,block_time,action,reason,venues) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                        (time.time(), s.chain, s.leader, s.side, s.token, str(s.token_amount),
                         s.quote_amount, s.signature, s.block_time, action, reason,
                         getattr(s, "venues", "")))
        self.db.commit()

    def open_position(self, p: Position) -> int:
        cur = self.db.execute("INSERT INTO positions(chain,token,leader,token_amount,cost,opened_at,"
                              "entry_sig,paper,status,timing) VALUES(?,?,?,?,?,?,?,?,?,?)",
                              (p.chain, p.token, p.leader, str(p.token_amount), p.cost,
                               p.opened_at, p.entry_sig, int(p.paper), p.status, json.dumps(p.timing)))
        self.db.commit()
        return cur.lastrowid

    def close_position(self, p: Position):
        self.db.execute("UPDATE positions SET status=?,exit_value=?,closed_at=?,exit_reason=?,exit_sig=?,"
                        "timing=? WHERE id=?", (p.status, p.exit_value, p.closed_at, p.exit_reason,
                                                p.exit_sig, json.dumps(p.timing), p.id))
        self.db.commit()

    def mark(self, p: Position, value: float):
        self.db.execute("INSERT INTO marks VALUES(?,?,?)", (p.id, time.time(), value))
        self.db.commit()

    def record_ticks(self, rows: list[tuple]):
        """Batch of pool updates: (ts, slot, token, venue, tok_res, sol_res, real_tok, real_sol)."""
        self.db.executemany("INSERT INTO ticks VALUES(?,?,?,?,?,?,?,?)", rows)
        self.db.commit()

    def record_event(self, ts, leader, sig, kind, token="", sol=None, detail=""):
        """A leader tx that did NOT become a signal, and why (small_buy, not_a_swap, not_signer...)."""
        self.db.execute("INSERT INTO leader_events VALUES(?,?,?,?,?,?,?)", (ts, leader, sig, kind, token, sol, detail))
        self.db.commit()

    def record_xtrade(self, t, leader: str, evm: str, via: str, detected_at: float | None = None) -> bool:
        """A leader's cross-chain trade (bot/xchain). False if this Relay request is already stored.
        detect_delay_s = when we learned of it - his deposit tx (buy: Solana, sell: EVM chain).
        Also logged in `signals` (quote_amount = USD, not SOL) so why.py & co. see it."""
        detected_at = detected_at or time.time()
        delay = detected_at - t.deposit_ts if t.deposit_ts else None
        cur = self.db.execute(
            "INSERT OR IGNORE INTO xchain_trades(request_id,ts,leader,evm_address,side,chain,chain_id,token,"
            "symbol,decimals,token_amount,usd,his_price_usd,status,deposit_tx,deposit_ts,fill_tx,fill_ts,"
            "detected_via,raw,detected_at,detect_delay_s) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (t.request_id, time.time(), leader, evm, t.side, t.chain, t.chain_id, t.token, t.symbol,
             t.decimals, str(t.token_amount), t.usd, t.price_usd, t.status, t.deposit_tx, t.deposit_ts,
             t.fill_tx, t.fill_ts, via, json.dumps(t.raw), detected_at, delay))
        if cur.rowcount == 0:
            return False
        sig = t.deposit_tx if t.side == "buy" else (t.fill_tx or t.request_id)
        self.db.execute("INSERT INTO signals(ts,chain,leader,side,token,token_amount,quote_amount,signature,"
                        "block_time,action,reason,venues) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                        (time.time(), t.chain, leader, t.side, t.token, str(t.token_amount), t.usd, sig,
                         int(t.deposit_ts) if t.deposit_ts else None, f"xchain_{t.side}",
                         f"watch-only {t.status}: {t.token_ui:,.6g} {t.symbol} for ${t.usd:,.2f} "
                         f"(USD, his price ${t.price_usd or 0:.6g})", "relay"))
        self.db.commit()
        return True

    def set_xprice(self, request_id: str, ts: float, price, impact, out_usd):
        """Relay /price ~15 s after his trade: what we would have paid/received (USD per token)."""
        self.db.execute("UPDATE xchain_trades SET price_later_ts=?, price_later_usd=?, price_later_impact_pct=?,"
                        " price_later_out_usd=? WHERE request_id=?", (ts, price, impact, out_usd, request_id))
        self.db.commit()

    def record_xprice(self, request_id: str, offset_s: float, ts: float, delay_s, price, gap, impact, out_usd):
        """One step of the price ladder: Relay /price for his size, offset_s after we detected his trade."""
        self.db.execute("INSERT INTO xchain_prices VALUES(?,?,?,?,?,?,?,?)",
                        (request_id, offset_s, ts, delay_s, price, gap, impact, out_usd))
        self.db.commit()

    def upsert_xaddress(self, leader: str, evm: str, ts: float, source: str) -> bool:
        """A leader's EVM address (lowercase). True if it is new."""
        new = self.db.execute("INSERT OR IGNORE INTO xchain_addresses VALUES(?,?,?,?,?)",
                              (leader, evm.lower(), ts, ts, source)).rowcount > 0
        if not new:
            self.db.execute("UPDATE xchain_addresses SET last_seen=MAX(last_seen, ?) WHERE leader=? AND evm_address=?",
                            (ts, leader, evm.lower()))
        self.db.commit()
        return new

    def xaddresses(self) -> dict[str, set[str]]:
        out: dict[str, set[str]] = {}
        for leader, evm in self.db.execute("SELECT leader, evm_address FROM xchain_addresses"):
            out.setdefault(leader, set()).add(evm)
        return out

    def xtrade_last_ts(self) -> dict[str, float]:
        """Per leader: time of his latest recorded cross-chain trade (decides the poll tier)."""
        return dict(self.db.execute("SELECT leader, MAX(COALESCE(deposit_ts, ts)) FROM xchain_trades GROUP BY leader"))

    def xtrade_count_from(self, evm: str, chain: str, since: float) -> int:
        """Relay requests that started on `chain` from `evm` (his sells / bridges there) since `since`."""
        return self.db.execute("SELECT COUNT(*) FROM xchain_trades WHERE evm_address=? AND chain=? "
                               "AND side IN ('sell','bridge') AND deposit_ts>=?", (evm, chain, since)).fetchone()[0]

    def xtrade_has_deposit(self, tx_hash: str) -> bool:
        """A Relay request with this deposit tx is already recorded (his EVM transfer was a Relay sell)."""
        return bool(self.db.execute("SELECT 1 FROM xchain_trades WHERE lower(deposit_tx)=? LIMIT 1",
                                    (tx_hash.lower(),)).fetchone())

    def update_position(self, p: Position):
        """Paper xchain position after an add-on buy or a partial sell (amount, cost, timing JSON)."""
        self.db.execute("UPDATE positions SET token_amount=?, cost=?, timing=? WHERE id=?",
                        (str(p.token_amount), p.cost, json.dumps(p.timing), p.id))
        self.db.commit()

    def record_xfill(self, **f) -> int:
        """One paper fill of an xchain position: his trade vs our Relay quote (bot/xchain/paper.py)."""
        cols = ("ts", "position_id", "chain", "token", "symbol", "side", "reason", "fraction", "his_ref",
                "his_ts", "his_usd", "his_price_usd", "our_usd", "our_tokens", "our_price_usd", "gap_pct",
                "delay_s", "impact_pct", "gas_usd", "leader", "detect_delay_s")
        cur = self.db.execute(f"INSERT INTO xchain_fills({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
                              tuple(str(f[c]) if c == "our_tokens" else f.get(c) for c in cols))
        self.db.commit()
        return cur.lastrowid

    def record_factor(self, token: str, ts: float, factor: float):
        """Calibration of a pool price against Jupiter (real value / pool-model value)."""
        self.db.execute("INSERT INTO pool_factors VALUES(?,?,?)", (ts, token, factor))
        self.db.commit()

    def set_ref(self, p: Position):
        self.db.execute("UPDATE positions SET ref_value=? WHERE id=?", (p.ref_value, p.id))
        self.db.commit()

    def load_open(self, paper: bool, chains=None) -> list[Position]:
        rows = self.db.execute("SELECT id,chain,token,leader,token_amount,cost,opened_at,entry_sig,ref_value,timing "
                               "FROM positions WHERE status!='closed' AND paper=?", (int(paper),))
        if chains is not None:
            rows = [r for r in rows if r[1] in chains]
        out = []
        for r in rows:
            p = Position(r[0], r[1], r[2], r[3], int(r[4]), r[5], r[6], r[7], paper)
            p.ref_value = r[8]          # restored stop reference (None for positions from before this patch)
            p.timing = json.loads(r[9]) if r[9] else {}
            peak = self.db.execute("SELECT MAX(value) FROM marks WHERE position_id=?", (r[0],)).fetchone()[0]
            p.peak_value = peak        # trailing stop keeps its peak across restarts
            out.append(p)
        return out

    def realized_pnl_today(self, chain: str, paper: bool) -> float:
        start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        r = self.db.execute("SELECT COALESCE(SUM(exit_value-cost),0) FROM positions WHERE chain=? "
                            "AND paper=? AND status='closed' AND closed_at>=?", (chain, int(paper), start))
        return r.fetchone()[0]

    def last_exit(self, chain: str, token: str) -> float | None:
        r = self.db.execute("SELECT MAX(closed_at) FROM positions WHERE chain=? AND token=?", (chain, token))
        return r.fetchone()[0]
