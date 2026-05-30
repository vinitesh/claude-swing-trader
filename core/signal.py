"""Signal and Position dataclasses — the lingua franca between layers."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class Action(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


@dataclass
class Signal:
    """A trading signal emitted by a strategy."""

    symbol: str
    action: Action
    entry_price: float
    stop_loss: float
    take_profit: float
    strategy_name: str
    confidence: float = 0.5         # 0..1
    timestamp: datetime = field(default_factory=datetime.utcnow)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def risk_per_share(self) -> float:
        return abs(self.entry_price - self.stop_loss)

    @property
    def reward_per_share(self) -> float:
        return abs(self.take_profit - self.entry_price)

    @property
    def reward_to_risk(self) -> float:
        return self.reward_per_share / self.risk_per_share if self.risk_per_share else 0.0


@dataclass
class Position:
    """An open position tracked by the engine/broker."""

    symbol: str
    qty: int
    avg_entry_price: float
    side: str                       # "long" | "short"
    strategy_name: str
    opened_at: datetime
    stop_loss: float | None = None
    take_profit: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def unrealized_pnl(self, current_price: float) -> float:
        sign = 1 if self.side == "long" else -1
        return sign * (current_price - self.avg_entry_price) * self.qty

    def unrealized_pnl_pct(self, current_price: float) -> float:
        if self.avg_entry_price == 0:
            return 0.0
        sign = 1 if self.side == "long" else -1
        return sign * (current_price - self.avg_entry_price) / self.avg_entry_price
