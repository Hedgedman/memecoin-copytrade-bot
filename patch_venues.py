"""Patch: (1) log which DEX/pool program each leader trade used,
(2) save the stop-loss reference point so it survives restarts.
Run from the copybot folder: python patch_venues.py"""

def edit(path, old, new, marker=None):
    s = open(path).read()
    if (marker or new) in s:
        print(f"{path}: already done"); return
    assert old in s, f"{path}: could not find the spot -> send this error to Claude:\n{old[:90]}"
    open(path, "w").write(s.replace(old, new, 1)); print(f"{path}: ok")

# ---------- models.py: signal remembers the venue ----------
edit("bot/models.py",
"    seen_at: float = field(default_factory=time.time)\n",
"    seen_at: float = field(default_factory=time.time)\n"
"    venues: str = \"\"         # DEX/pool programs the leader's trade went through\n")

# ---------- store.py: new columns + saving/restoring the stop reference ----------
edit("bot/store.py",
'        """)\n        self.db.commit()\n',
'''        """)
        self.db.commit()
        for table, col, typ in (("signals", "venues", "TEXT"), ("positions", "ref_value", "REAL")):
            try:
                self.db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
            except sqlite3.OperationalError:
                pass   # column already exists
        self.db.commit()
''', marker='("positions", "ref_value", "REAL")')

edit("bot/store.py",
'''        self.db.execute("INSERT INTO signals(ts,chain,leader,side,token,token_amount,quote_amount,"
                        "signature,block_time,action,reason) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (time.time(), s.chain, s.leader, s.side, s.token, str(s.token_amount),
                         s.quote_amount, s.signature, s.block_time, action, reason))''',
'''        self.db.execute("INSERT INTO signals(ts,chain,leader,side,token,token_amount,quote_amount,"
                        "signature,block_time,action,reason,venues) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                        (time.time(), s.chain, s.leader, s.side, s.token, str(s.token_amount),
                         s.quote_amount, s.signature, s.block_time, action, reason,
                         getattr(s, "venues", "")))''')

edit("bot/store.py",
"    def load_open(self, paper: bool) -> list[Position]:\n",
'''    def set_ref(self, p: Position):
        self.db.execute("UPDATE positions SET ref_value=? WHERE id=?", (p.ref_value, p.id))
        self.db.commit()

    def load_open(self, paper: bool) -> list[Position]:
''', marker="def set_ref(")

edit("bot/store.py",
'''        rows = self.db.execute("SELECT id,chain,token,leader,token_amount,cost,opened_at,entry_sig "
                               "FROM positions WHERE status!='closed' AND paper=?", (int(paper),))
        return [Position(r[0], r[1], r[2], r[3], int(r[4]), r[5], r[6], r[7], paper) for r in rows]''',
'''        rows = self.db.execute("SELECT id,chain,token,leader,token_amount,cost,opened_at,entry_sig,ref_value "
                               "FROM positions WHERE status!='closed' AND paper=?", (int(paper),))
        out = []
        for r in rows:
            p = Position(r[0], r[1], r[2], r[3], int(r[4]), r[5], r[6], r[7], paper)
            p.ref_value = r[8]          # restored stop reference (None for positions from before this patch)
            out.append(p)
        return out''')

# ---------- positions.py: save the reference point when it is first set ----------
edit("bot/positions.py",
'''        if p.ref_value is None:
            p.ref_value = value          # first exit quote after buying = our "entry price"
''',
'''        if p.ref_value is None:
            p.ref_value = value          # first exit quote after buying = our "entry price"
            self.store.set_ref(p)        # saved, so a restart keeps the same stop level
''')

# ---------- solana.py: recognise DEX programs in a transaction ----------
edit("bot/chains/solana.py",
"def parse_stable_swap(",
'''# Known Solana trading programs, used only for LABELLING trades (not for trading).
# Anything not listed is logged as "?" + the first characters of its program id.
VENUE_PROGRAMS = {
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P": "pumpfun_curve",
    "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA": "pumpswap_amm",
    "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8": "raydium_amm_v4",
    "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C": "raydium_cpmm",
    "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK": "raydium_clmm",
    "LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj": "raydium_launchlab",
    "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo": "meteora_dlmm",
    "Eo7WjKq67rjJQSZxS6z3YkapzY3eMj6Xy8X5EQVn5UaB": "meteora_damm_v1",
    "cpamdpZCGKUy5JxQXB4dcpGPiikHawvSWAd6mEn1sGG": "meteora_damm_v2",
    "dbcij3LWUppWqq96dh6gJWwBifmcGfLSB5D4DuSMaqN": "meteora_dbc",
    "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc": "orca_whirlpool",
    "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4": "jupiter",
}
# Infrastructure programs that appear in almost every transaction: never interesting.
INFRA_PROGRAMS = {
    "11111111111111111111111111111111", "ComputeBudget111111111111111111111111111111",
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA", "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",
    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL", "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr",
    "Memo1UhkJRfHyvLMcVucJwxXeuD728EqVDDwQDxFMNo",
}


def venues_in_tx(tx: dict | None) -> str:
    """Comma-separated list of trading programs a transaction went through."""
    if not tx:
        return ""
    ids = set()
    for ix in tx.get("transaction", {}).get("message", {}).get("instructions", []) or []:
        ids.add(ix.get("programId"))
    for inner in (tx.get("meta") or {}).get("innerInstructions") or []:
        for ix in inner.get("instructions", []) or []:
            ids.add(ix.get("programId"))
    names = set()
    for pid in ids:
        if not pid or pid in INFRA_PROGRAMS:
            continue
        names.add(VENUE_PROGRAMS.get(pid, "?" + pid[:6]))
    return ",".join(sorted(names))


def parse_stable_swap(''', marker="def venues_in_tx(")

edit("bot/chains/solana.py",
'''        await queue.put(Signal(self.name, leader, side, mint, amount, sol, sig, tx.get("blockTime")))''',
'''        s = Signal(self.name, leader, side, mint, amount, sol, sig, tx.get("blockTime"))
        s.venues = venues_in_tx(tx)
        await queue.put(s)''')

# ---------- main.py: show the venue in the signal line ----------
edit("bot/main.py",
'''            log.info("signal %s %s %s %.3f by %s", s.chain, s.side, s.token, s.quote_amount, s.leader[:8])''',
'''            log.info("signal %s %s %s %.3f by %s via %s", s.chain, s.side, s.token, s.quote_amount,
                     s.leader[:8], getattr(s, "venues", "") or "-")''')
print("DONE")
