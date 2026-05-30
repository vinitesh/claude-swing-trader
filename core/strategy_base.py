"""Abstract base class every strategy implements.

Adding a new strategy = creating one file in `strategies/` that subclasses
`Strategy` and implements the four abstract methods. The registry auto-
discovers it; no core changes needed.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

import pandas as pd

from core.signal import Position, Signal

if TYPE_CHECKING:
    pass


class Strategy(ABC):
    """A pluggable trading strategy.

    Subclasses MUST set the class attribute `name`.
    """

    name: str = "BaseStrategy"

    def __init__(self, config: dict):
        self.config: dict = config
        self.allocation_pct: float = float(config.get("allocation_pct", 0.2))
        self.risk_pct: float = float(config.get("risk_pct", 0.01))

    # ---------------- Universe ----------------
    @abstractmethod
    def universe(self) -> list[str]:
        """Symbols this strategy trades."""
        ...

    # ---------------- Indicators ----------------
    @abstractmethod
    def indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        """Augment a per-symbol bars DataFrame with indicator columns.

        The DataFrame is indexed by date and has at least:
            open, high, low, close, volume

        Implementations should:
          - Mutate or return a copy with extra columns (ema20, rsi, etc.)
          - Be deterministic and look-ahead-bias free
        """
        ...

    # ---------------- Entry ----------------
    @abstractmethod
    def should_enter(self, df: pd.DataFrame) -> Signal | None:
        """Inspect the latest bar and decide whether to enter.

        Returns a Signal if entry is warranted, else None.
        The bar to "act on" is df.iloc[-1] — implementations should NOT
        peek at any later bar (no look-ahead).
        """
        ...

    # ---------------- Exit ----------------
    def should_exit(self, position: Position, df: pd.DataFrame) -> bool:
        """Decide whether to flatten a position based on its current state.

        Default: never. The Engine separately enforces bracket-order
        stop-loss / take-profit. Override only if you need extra logic
        like a time stop or signal reversal.
        """
        return False

    # ---------------- Position sizing ----------------
    def position_size(
        self, account_value: float, entry_price: float, stop_loss: float
    ) -> int:
        """Default position sizing: fixed-fractional risk.

        position_size = (account * risk_pct) / per_share_risk
        """
        per_share_risk = abs(entry_price - stop_loss)
        if per_share_risk <= 0 or account_value <= 0 or entry_price <= 0:
            return 0
        risk_dollars = account_value * self.risk_pct
        qty = int(risk_dollars // per_share_risk)
        # Cap by allocation_pct
        max_dollars = account_value * self.allocation_pct
        max_qty_by_alloc = int(max_dollars // entry_price)
        return max(0, min(qty, max_qty_by_alloc))

    # ---------------- Helpers ----------------
    def __repr__(self) -> str:  # pragma: no cover
        return f"<Strategy name={self.name} alloc={self.allocation_pct}>"
