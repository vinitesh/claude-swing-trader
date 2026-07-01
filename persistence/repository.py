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
    position_id: int | None = None,
) -> Position | None:
    """Close ONE open position row and stamp its realized P&L.

    A symbol can legitimately have more than one open row (e.g. a strategy
    re-entered a symbol it already held, or two strategies both hold it). The
    broker collapses those into a single net position, so a naive
    ``scalar_one_or_none()`` here raised ``MultipleResultsFound`` and rolled
    back the ENTIRE sync transaction — leaving every reconciled exit unrecorded
    and the DB drifting from the broker.

    To stay robust:
      * If ``position_id`` is given, close exactly that row (deterministic —
        the caller iterating per-row uses this).
      * Otherwise close the OLDEST open row for the symbol (``.first()``, never
        ``scalar_one_or_none``), so a duplicate can never wedge the caller.
    """
    stmt = select(Position).where(Position.is_open == True)  # noqa: E712
    if position_id is not None:
        stmt = stmt.where(Position.id == position_id)
    else:
        stmt = stmt.where(Position.symbol == symbol).order_by(Position.opened_at.asc())
    p = session.execute(stmt).scalars().first()
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
    # Oldest-first: sync relies on this order so that when a symbol has
    # duplicate open rows, the OLDEST is the one that receives the real exit
    # fill and later duplicates are closed flat (no double-counted P&L).
    return list(
        session.execute(
            select(Position)
            .where(Position.is_open == True)  # noqa: E712
            .order_by(Position.opened_at.asc(), Position.id.asc())
        ).scalars()
    )


def get_strategy_for_symbol(session: Session, symbol: str) -> str | None:
    """Find the strategy that opened the currently-open position on this symbol."""
    p = session.execute(
        select(Position).where(Position.symbol == symbol, Position.is_open == True)  # noqa: E712
    ).scalars().first()
    return p.strategy_name if p else None


def has_open_position(session: Session, symbol: str) -> bool:
    """True if ANY open position row already exists for ``symbol``.

    The broker nets all activity in a symbol into ONE position, so a second
    DB open row for a symbol we already hold can never be reconciled 1:1 by
    sync (it would see a single broker position for two DB rows). The live
    runner uses this to refuse a duplicate entry rather than create an
    unsyncable row.
    """
    return session.execute(
        select(Position.id).where(
            Position.symbol == symbol, Position.is_open == True  # noqa: E712
        ).limit(1)
    ).first() is not None


def classify_for_sync(
    db_open_symbols: list[str],
    held_symbols: set[str],
    pending_symbols: set[str],
) -> dict[str, list[str]]:
    """Pure reconciliation decision for `sync` — no DB access, fully testable.

    Given the symbols currently open in our DB and what the broker reports
    (positions it HOLDS, and symbols with a WORKING order), bucket each DB
    symbol into exactly one action:

      - 'keep_held'      : broker still holds it          → leave open
      - 'keep_pending'   : not held, but an order is working (entry queued or
                            resting bracket leg) → leave open, do NOT close
      - 'close'          : broker neither holds it nor has a working order
                            → genuinely gone, safe to close

    The 'keep_pending' bucket is the fix for the desync bug: an after-close
    run-live queues orders that fill next open; closing them at the 19:00 sync
    (before they fill) wrongly wrote them off and blinded the capital cap.
    """
    buckets: dict[str, list[str]] = {"keep_held": [], "keep_pending": [], "close": []}
    for sym in db_open_symbols:
        if sym in held_symbols:
            buckets["keep_held"].append(sym)
        elif sym in pending_symbols:
            buckets["keep_pending"].append(sym)
        else:
            buckets["close"].append(sym)
    return buckets


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
