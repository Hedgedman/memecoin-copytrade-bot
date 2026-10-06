from __future__ import annotations
import time
from dataclasses import dataclass, field


@dataclass
class Signal:
    """A leader trade detected on-chain."""
    chain: str
    leader: str
    side: str            # "buy" | "sell"
    token: str
    token_amount: int    # raw units
    quote_amount: float  # native units (SOL / ETH) spent or received by leader
    signature: str
    block_time: int | None
    seen_at: float = field(default_factory=time.time)
    venues: str = ""         # DEX/pool programs the leader's trade went through
    notified_at: float | None = None   # websocket told us about the leader tx
    fetched_at: float | None = None    # full leader tx downloaded and parsed
    slot: int | None = None            # slot the leader tx landed in


@dataclass
class Fill:
    ok: bool
    signature: str | None
    token_amount: int      # raw units received (buy) or sold (sell)
    quote_amount: float    # native units spent (buy) or received (sell), fees included
    error: str | None = None
    slot: int | None = None   # slot of the pool state we filled on (paper) / landed in (live)
    model: str = ""           # how the fill was produced, e.g. "pumpfun_curve+1.5s", "jupiter+1.5s", "live"


@dataclass
class Position:
    id: int | None
    chain: str
    token: str
    leader: str
    token_amount: int
    cost: float            # native units spent incl. fees
    opened_at: float
    entry_sig: str | None
    paper: bool
    status: str = "open"   # open | closing | closed
    exit_value: float | None = None
    closed_at: float | None = None
    exit_reason: str | None = None
    exit_sig: str | None = None
    sell_attempts: int = 0
    quote_failures: int = 0
    ref_value: float | None = None   # first exit quote after buying = entry price for the stop
    timing: dict = field(default_factory=dict)   # stage timestamps + slots, saved as JSON
    last_mark_at: float = 0.0                    # not saved; throttles writes to `marks`
    peak_value: float | None = None              # highest exit value seen (trailing stop); rebuilt from marks
