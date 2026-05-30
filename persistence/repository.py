"""Thin repository layer — keeps SQLAlchemy out of business code.

Each function takes a Session (caller controls transaction boundary via
session_scope()), and returns plain dicts/ORM rows. We deliberately don't
return DTOs because the consumers (CLI, tests) are happy with rows.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from core.signal import Signal as DomainSignal
from persistence.models import Alert, Order, Position, Run, Signal


# ---------------- Runs ----------------
def start_run(session: Session, mode: str, strategies: list[str]) -> Run:
    r = Run(
        started_at=datetime.utcnow(),
        mode=mode,
        strategies_run=",".join(strategies),
    )
    session.add(r)
    session.flush()
    return r


def finish_run(
    session: Session,
    run: Run,
    signals_found: int,
    orders_submitted: int,
    orders_skipped: int,
    orders_rejected: int,
    error: str | None = None,
    notes: str | None = None,
) -> None:
    run.finished_at = datetime.utcnow()
    run.signals_found = signals_found
    run.orders_submitted = orders_submitted
    run.orders_skipped = orders_skipped
    run.orders_rejected = orders_rejected
    run.error = error
    run.notes = notes


# ---------------- Signals ----------------
def record_signal(
    session: Session,
    run: Run | None,
    domain_signal: DomainSignal,
    bar_date: date,
) -> tuple[Signal | None, bool]:
    """Insert a signal row. Returns (row, is_new). On duplicate (idempotency),
    returns (existing_row, False) without raising.
    """
    bar_dt = datetime.combine(bar_date, datetime.min.time())
    row = Signal(
        run_id=run.id if run is not None else None,
        bar_date=bar_dt,
        strategy_name=domain_signal.strategy_name,
        symbol=domain_signal.symbol,
        action=domain_signal.action.value,
        entry_price=float(domain_signal.entry_price),
        stop_loss=float(domain_signal.stop_loss),
        take_profit=float(domain_signal.take_profit),
        confidence=float(domain_signal.confidence),
        metadata_json=json.dumps(domain_signal.metadata, default=str),
    )
    # Use a SAVEPOINT so a duplicate-key error doesn't kill the outer transaction.
    sp = session.begin_nested()
    try:
        session.add(row)
        session.flush()
        sp.commit()
        return row, True
    except IntegrityError:
        sp.rollback()
        existing = session.execute(
            select(Signal).where(
                Signal.bar_date == bar_dt,
                Signal.strategy_name == domain_signal.strategy_name,
                Signal.symbol == domain_signal.symbol,
            )
        ).scalar_one_or_none()
        return existing, False


def signal_already_acted_today(
    session: Session,
    strategy_name: str,
    symbol: str,
    on_date: date,
) -> bool:
    """True if a signal for (strategy, symbol, date) already produced an order.

    Used by run-live to skip duplicate submissions on re-runs.
    """
    bar_dt = datetime.combine(on_date, datetime.min.time())
    sig = session.execute(
        select(Signal).where(
            Signal.bar_date == bar_dt,
            Signal.strategy_name == strategy_name,
            Signal.symbol == symbol,
        )
    ).scalar_one_or_none()
    if sig is None:
        return False
    has_order = session.execute(
        select(Order).where(Order.signal_id == sig.id, Order.status != "rejected")
    ).first()
    return has_order is not None


# ---------------- Orders ----------------
def record_order_submitted(
    session: Session,
    signal_row: Signal | None,
    broker: str,
    broker_order_id: str | None,
    symbol: str,
    side: str,
    qty: int,
    entry_price: float,
    stop_loss: float,
    take_profit: float,
    status: str = "submitted",
    error: str | None = None,
) -> Order:
    o = Order(
        signal_id=signal_row.id if signal_row is not None else None,
        broker=broker,
        broker_order_id=broker_order_id,
        symbol=symbol,
        side=side,
        qty=qty,
        entry_price=entry_price,
        stop_loss=stop_loss,
        take_profit=take_profit,
        status=status,
        error=error,
    )
    session.add(o)
    session.flush()
    return o


def update_order_status(
    session: Session,
    broker_order_id: str,
    status: str,
    fill_price: float | None = None,
    filled_at: datetime | None = None,
    error: str | None = None,
) -> Order | None:
    o = session.execute(
        select(Order).where(Order.broker_order_id == broker_order_id)
    ).scalar_one_or_none()
    if o is None:
        return None
    o.status = status
    if fill_price is not None:
        o.fill_price = fill_price
    if filled_at is not None:
        o.filled_at = filled_at
    if error is not None:
        o.error = error
    session.flush()
    return o


# ---------------- Positions ----------------
def open_position(
    session: Session,
    *,
    symbol: str,
    strategy_name: str,
    side: str,
    qty: int,
    avg_entry_price: float,
    stop_loss: float | None,
    take_profit: float | None,
) -> Position:
    p = Position(
        symbol=symbol,
        strategy_name=strategy_name,
        side=side,
        qty=qty,
        avg_entry_price=avg_entry_price,
        stop_loss=stop_loss,
        take_profit=take_profit,
        is_open=True,
    )
    session.add(p)
    session.flush()
    return p


def close_position(
    session: Session,
    *,
    symbol: str,
    exit_price: float,
    exit_reason: str,
    closed_at: datetime | None = None,
) -> Position | None:
    p = session.execute(
        select(Position).where(Position.symbol == symbol, Position.is_open == True)  # noqa: E712
    ).scalar_one_or_none()
    if p is None:
        return None
    sign = 1 if p.side == "long" else -1
    p.exit_price = exit_price
    p.exit_reason = exit_reason
    p.closed_at = closed_at or datetime.utcnow()
    p.realized_pnl = sign * (exit_price - p.avg_entry_price) * p.qty
    p.is_open = False
    return p


def get_open_positions(session: Session) -> list[Position]:
    return list(
        session.execute(select(Position).where(Position.is_open == True)).scalars()  # noqa: E712
    )


def get_strategy_for_symbol(session: Session, symbol: str) -> str | None:
    """Find the strategy that opened the currently-open position on this symbol."""
    p = session.execute(
        select(Position).where(Position.symbol == symbol, Position.is_open == True)  # noqa: E712
    ).scalar_one_or_none()
    return p.strategy_name if p else None


# ---------------- Alerts ----------------
def record_alert(
    session: Session,
    *,
    channel: str,
    kind: str,
    body: str,
    delivered: bool,
    error: str | None = None,
) -> Alert:
    a = Alert(channel=channel, kind=kind, body=body, delivered=delivered, error=error)
    session.add(a)
    session.flush()
    return a


# ---------------- Reporting ----------------
def daily_pnl_realized(session: Session, on_date: date | None = None) -> float:
    """Sum realized P&L of positions closed on this date."""
    on_date = on_date or date.today()
    start = datetime.combine(on_date, datetime.min.time())
    end = start + timedelta(days=1)
    rows = session.execute(
        select(Position).where(
            Position.closed_at >= start, Position.closed_at < end, Position.is_open == False  # noqa: E712
        )
    ).scalars().all()
    return float(sum((r.realized_pnl or 0.0) for r in rows))
