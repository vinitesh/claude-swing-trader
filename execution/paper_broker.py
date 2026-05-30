"""In-memory simulated broker — used by the backtester.

It does NOT model slippage or partial fills. Bracket orders are simulated
by tracking stop_loss / take_profit on each Position; the backtester is
responsible for triggering exits when daily bars cross those levels.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from uuid import uuid4

from core.signal import Position, Signal
from execution.broker_base import Account, Broker


@dataclass
class _SimAccount:
    cash: float
    equity: float
    realized_pnl: float = 0.0
    positions: dict[str, Position] = field(default_factory=dict)


class PaperBroker(Broker):
    """Backtest broker: holds cash + positions in memory."""

    name = "paper"

    def __init__(self, starting_cash: float = 100_000.0):
        self.starting_cash = float(starting_cash)
        self._sim = _SimAccount(cash=starting_cash, equity=starting_cash)

    # ---- Read ----
    def get_account(self) -> Account:
        return Account(
            cash=self._sim.cash,
            equity=self._sim.equity,
            buying_power=self._sim.cash,
        )

    def get_positions(self) -> list[Position]:
        return list(self._sim.positions.values())

    def get_positions_dict(self) -> dict[str, Position]:
        return dict(self._sim.positions)

    # ---- Write ----
    def submit_bracket_order(self, signal: Signal, qty: int) -> str:
        """Simulated bracket entry: opens a position immediately at entry_price."""
        if qty <= 0:
            raise ValueError(f"Invalid qty {qty}")
        cost = signal.entry_price * qty
        if cost > self._sim.cash:
            raise ValueError(
                f"Insufficient cash: need {cost:.2f}, have {self._sim.cash:.2f}"
            )

        side = "long" if signal.action.value == "BUY" else "short"
        pos = Position(
            symbol=signal.symbol,
            qty=qty,
            avg_entry_price=signal.entry_price,
            side=side,
            strategy_name=signal.strategy_name,
            opened_at=signal.timestamp or datetime.utcnow(),
            stop_loss=signal.stop_loss,
            take_profit=signal.take_profit,
            metadata={"order_id": str(uuid4())},
        )
        self._sim.cash -= cost
        self._sim.positions[signal.symbol] = pos
        return pos.metadata["order_id"]

    def close_position(self, symbol: str, exit_price: float | None = None) -> float:
        """Flatten a position; returns realized PnL.

        If exit_price is None, uses the avg_entry_price (pnl = 0). The
        backtester always passes an explicit price.
        """
        pos = self._sim.positions.pop(symbol, None)
        if pos is None:
            return 0.0
        price = exit_price if exit_price is not None else pos.avg_entry_price
        pnl = pos.unrealized_pnl(price)
        self._sim.cash += pos.qty * price
        self._sim.realized_pnl += pnl
        return pnl

    # ---- Mark-to-market helpers (used by the backtester) ----
    def mark_to_market(self, prices: dict[str, float]) -> None:
        equity = self._sim.cash
        for sym, pos in self._sim.positions.items():
            px = prices.get(sym, pos.avg_entry_price)
            equity += pos.qty * px
        self._sim.equity = equity

    @property
    def realized_pnl(self) -> float:
        return self._sim.realized_pnl
