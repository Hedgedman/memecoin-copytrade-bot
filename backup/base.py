from __future__ import annotations
import asyncio
from abc import ABC, abstractmethod
from typing import Callable
from ..models import Fill, Position


class RateLimited(Exception):
    """A price/route API refused the request because we called it too often."""


class ChainAdapter(ABC):
    """Everything chain-specific lives behind this interface.

    Core logic (risk, positions, logging) never touches RPCs directly, so adding
    an EVM chain (Robinhood Chain, BSC, Base) means writing one new adapter.
    """
    name: str

    @abstractmethod
    async def start(self) -> None: ...

    @abstractmethod
    async def close(self) -> None: ...

    @abstractmethod
    async def listen(self, queue: asyncio.Queue) -> None:
        """Stream leader trades as Signal objects into queue. Must reconnect forever."""

    @abstractmethod
    async def pre_trade_check(self, token: str, amount_native: float) -> tuple[bool, str]:
        """Cheap safety checks before buying (freeze authority, round-trip loss)."""

    @abstractmethod
    async def buy(self, token: str, amount_native: float) -> Fill: ...

    @abstractmethod
    async def quote_exit(self, pos: Position) -> float | None:
        """Native value we'd receive selling the whole position now (slippage included)."""

    @abstractmethod
    async def sell(self, pos: Position, slippage_bps: int | None = None) -> Fill: ...

    # ---- optional: push-based pricing. Adapters without it keep being polled. ----
    def set_price_callback(self, cb: Callable[[str], None]) -> None:
        """cb(token) is called whenever a watched token's price changes."""

    def set_tick_sink(self, sink: Callable[[list], None]) -> None:
        """sink(rows) receives batches of pool updates for the `ticks` table."""

    def set_factor_sink(self, sink: Callable[[str, float, float], None]) -> None:
        """sink(token, ts, factor) receives pool-price calibrations (real / model)."""

    async def track(self, token: str) -> None:
        """A position in `token` is open: keep its price live."""

    async def release(self, token: str) -> None:
        """Position closed: keep recording for a while (tick tail), then stop."""

    async def observe(self, token: str) -> None:
        """A skipped signal: record its price path anyway (research data)."""

    def fill_market_cap(self, token: str, fill: Fill) -> float | None:
        """Market cap (USD) at the average price of this fill, or None if unknown."""
        return None
