"""Per-strategy reporting: slice the trade log by strategy and compute metrics.

Reads from the live SQLite DB (positions, orders, signals, runs tables) and
produces both a printable Rich table and a structured dict for machine use.

Metrics per strategy:
  - signals_total      : how many signals scanner has emitted (lifetime)
  - orders_submitted   : how many orders went to the broker (lifetime)
  - positions_open     : currently open per our DB
  - positions_closed   : lifetime closed
  - realized_pnl       : sum of realized_pnl on closed positions
  - win_rate           : closed positions where realized_pnl > 0
  - avg_win / avg_loss : in dollars
  - profit_factor      : gross_profit / gross_loss (∞ if no losses)
  - avg_hold_days      : mean (closed_at - opened_at)
  - last_run_at        : most recent run that touched this strategy

Caveat: realized_pnl is only as good as the sync layer keeps it. For Alpaca
paper, run `swingbot sync` after market close to refresh.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import select, func
from sqlalchemy.orm import Session

from persistence.models import Order, Position, Signal


@dataclass
class StrategyReport:
    strategy_name: str
    signals_total: int = 0
    orders_submitted: int = 0
    positions_open: int = 0
    positions_closed: int = 0
    realized_pnl: float = 0.0
    win_rate: float = 0.0
    num_wins: int = 0
    num_losses: int = 0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    profit_factor: float = 0.0
    avg_hold_days: float = 0.0
    last_run_at: datetime | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "strategy_name": self.strategy_name,
            "signals_total": self.signals_total,
            "orders_submitted": self.orders_submitted,
            "positions_open": self.positions_open,
            "positions_closed": self.positions_closed,
            "realized_pnl": round(self.realized_pnl, 2),
            "win_rate": round(self.win_rate, 3),
            "num_wins": self.num_wins,
            "num_losses": self.num_losses,
            "avg_win": round(self.avg_win, 2),
            "avg_loss": round(self.avg_loss, 2),
            "profit_factor": round(self.profit_factor, 2) if self.profit_factor != float("inf") else None,
            "avg_hold_days": round(self.avg_hold_days, 1),
            "last_run_at": self.last_run_at.isoformat() if self.last_run_at else None,
        }


def report_strategy(session: Session, strategy_name: str) -> StrategyReport:
    r = StrategyReport(strategy_name=strategy_name)

    # Signals
    r.signals_total = int(session.execute(
        select(func.count(Signal.id)).where(Signal.strategy_name == strategy_name)
    ).scalar() or 0)

    # Orders submitted (any non-rejected)
    r.orders_submitted = int(session.execute(
        select(func.count(Order.id))
        .join(Signal, Order.signal_id == Signal.id)
        .where(Signal.strategy_name == strategy_name, Order.status != "rejected")
    ).scalar() or 0)

    # Positions: open + closed
    r.positions_open = int(session.execute(
        select(func.count(Position.id)).where(
            Position.strategy_name == strategy_name, Position.is_open == True  # noqa: E712
        )
    ).scalar() or 0)

    closed_positions = list(session.execute(
        select(Position).where(
            Position.strategy_name == strategy_name, Position.is_open == False  # noqa: E712
        )
    ).scalars())

    r.positions_closed = len(closed_positions)
    if not closed_positions:
        return r

    # PnL aggregates
    pnls = [(p.realized_pnl or 0.0) for p in closed_positions]
    r.realized_pnl = sum(pnls)
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    r.num_wins = len(wins)
    r.num_losses = len(losses)
    r.win_rate = r.num_wins / r.positions_closed if r.positions_closed else 0.0
    gross_profit = sum(wins)
    gross_loss = -sum(losses)
    if gross_loss > 0:
        r.profit_factor = gross_profit / gross_loss
    elif gross_profit > 0:
        r.profit_factor = float("inf")
    r.avg_win = (gross_profit / r.num_wins) if r.num_wins else 0.0
    r.avg_loss = (-gross_loss / r.num_losses) if r.num_losses else 0.0

    # Hold time
    holds = []
    for p in closed_positions:
        if p.opened_at and p.closed_at:
            holds.append((p.closed_at - p.opened_at).total_seconds() / 86400.0)
    r.avg_hold_days = sum(holds) / len(holds) if holds else 0.0

    # Most recent run touching this strategy
    from persistence.models import Run
    last = session.execute(
        select(Run).where(Run.strategies_run.like(f"%{strategy_name}%")).order_by(Run.started_at.desc()).limit(1)
    ).scalar_one_or_none()
    r.last_run_at = last.started_at if last else None

    return r


def report_all_strategies(session: Session) -> list[StrategyReport]:
    """Return one report per strategy that is either enabled in YAML config OR
    has touched the DB. New strategies show as zero rows so the bake-off table
    surfaces them immediately, before they've traded.
    """
    names: list[str] = []

    # Strategies with DB activity (signals or positions)
    for (n,) in session.execute(select(Signal.strategy_name).distinct()).all():
        if n not in names:
            names.append(n)
    for (n,) in session.execute(select(Position.strategy_name).distinct()).all():
        if n not in names:
            names.append(n)

    # Strategies enabled in YAML — surface even before first signal
    try:
        from core.config import load_global_config
        cfg = load_global_config()
        for entry in (cfg.get("strategies") or []):
            if not entry.get("enabled", True):
                continue
            n = entry.get("name")
            if n and n not in names:
                names.append(n)
    except Exception:
        # Config loading shouldn't break the report; fall through with DB names.
        pass

    return [report_strategy(session, n) for n in sorted(names)]
