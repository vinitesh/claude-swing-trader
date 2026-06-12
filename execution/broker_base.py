"""Abstract broker — keeps strategy code broker-agnostic."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from core.signal import Position, Signal


@dataclass
class Account:
    cash: float
    equity: float
    buying_power: float


class Broker(ABC):
    name: str = "base"

    @abstractmethod
    def get_account(self) -> Account: ...

    @abstractmethod
    def get_positions(self) -> list[Position]: ...

    @abstractmethod
    def submit_bracket_order(self, signal: Signal, qty: int) -> str:
        """Submit a market entry with attached stop-loss + take-profit. Returns order id."""
        ...

    @abstractmethod
    def close_position(self, symbol: str) -> None: ...

    def cancel_all_orders(self) -> int:
        """Cancel all open orders. Default: no-op (override for real brokers)."""
        return 0

    def get_open_order_symbols(self) -> set[str]:
        """Symbols with a working (non-terminal) order. Default: none."""
        return set()

    def get_last_exit_fill(
        self, symbol: str, opened_after: datetime | None = None
    ) -> tuple[float, datetime] | None:
        """(fill_price, filled_at) of the position's exit sell. Default: unknown."""
        return None

    def is_market_open(self) -> bool:
        """Default: always open (override for real brokers)."""
        return True
