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
        CREATE INDEX IF NOT EXISTS pool_factors_token ON pool_factors(token, ts);
        """)
        self.db.commit()
        for table, col, typ in (("signals", "venues", "TEXT"), ("positions", "ref_value", "REAL"),
                               ("positions", "timing", "TEXT")):
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

    def record_factor(self, token: str, ts: float, factor: float):
        """Calibration of a pool price against Jupiter (real value / pool-model value)."""
        self.db.execute("INSERT INTO pool_factors VALUES(?,?,?)", (ts, token, factor))
        self.db.commit()

    def set_ref(self, p: Position):
        self.db.execute("UPDATE positions SET ref_value=? WHERE id=?", (p.ref_value, p.id))
        self.db.commit()

    def load_open(self, paper: bool) -> list[Position]:
        rows = self.db.execute("SELECT id,chain,token,leader,token_amount,cost,opened_at,entry_sig,ref_value,timing "
                               "FROM positions WHERE status!='closed' AND paper=?", (int(paper),))
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
