"""SQLAlchemy ORM models for the live trading log.

Schema rationale:
  - signals     : every signal emitted (whether or not we acted on it)
  - orders      : every Alpaca order we submitted (with their order_id for sync)
  - positions   : strategy↔symbol mapping that Alpaca itself doesn't track,
                  plus realized PnL when closed
  - runs        : one row per `swingbot run-live` invocation, used for
                  idempotency (date+strategy+symbol unique on signals)
  - alerts      : Telegram delivery audit (for debugging when notifications
                  go missing)

Money: stored as float for simplicity. Real shop would use Numeric. The
P&L numbers are an audit log, not source of truth — Alpaca is.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    Index,
)
from sqlalchemy.orm import DeclarativeBase, relationship


class Base(DeclarativeBase):
    pass


class Run(Base):
    __tablename__ = "runs"
    id = Column(Integer, primary_key=True)
    started_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    finished_at = Column(DateTime, nullable=True)
    mode = Column(String(16), nullable=False)          # paper | live | dry-run
    strategies_run = Column(String(256), nullable=False)
    signals_found = Column(Integer, default=0)
    orders_submitted = Column(Integer, default=0)
    orders_skipped = Column(Integer, default=0)         # idempotent dedupe
    orders_rejected = Column(Integer, default=0)        # risk-manager rejects
    error = Column(String(512), nullable=True)
    notes = Column(String(512), nullable=True)


class Signal(Base):
    __tablename__ = "signals"
    id = Column(Integer, primary_key=True)
    run_id = Column(Integer, ForeignKey("runs.id"), nullable=True, index=True)
    bar_date = Column(DateTime, nullable=False, index=True)   # the bar the signal was based on
    strategy_name = Column(String(64), nullable=False)
    symbol = Column(String(16), nullable=False)
    action = Column(String(8), nullable=False)
    entry_price = Column(Float, nullable=False)
    stop_loss = Column(Float, nullable=False)
    take_profit = Column(Float, nullable=False)
    confidence = Column(Float, default=0.5)
    metadata_json = Column(String(2048), default="{}")

    __table_args__ = (
        # A given (date, strategy, symbol) must be unique — backbone of idempotency.
        UniqueConstraint("bar_date", "strategy_name", "symbol", name="uq_signal_dsyssymbol"),
        Index("ix_signal_strategy_symbol", "strategy_name", "symbol"),
    )

    orders = relationship("Order", back_populates="signal")


class Order(Base):
    __tablename__ = "orders"
    id = Column(Integer, primary_key=True)
    signal_id = Column(Integer, ForeignKey("signals.id"), nullable=True)
    submitted_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    broker = Column(String(16), nullable=False, default="alpaca")
    broker_order_id = Column(String(64), nullable=True, unique=True)
    symbol = Column(String(16), nullable=False, index=True)
    side = Column(String(8), nullable=False)           # buy | sell
    qty = Column(Integer, nullable=False)
    entry_price = Column(Float, nullable=False)        # intended; actual fill differs
    stop_loss = Column(Float, nullable=False)
    take_profit = Column(Float, nullable=False)
    status = Column(String(16), nullable=False, default="submitted")  # submitted|filled|rejected|canceled
    filled_at = Column(DateTime, nullable=True)
    fill_price = Column(Float, nullable=True)
    error = Column(String(512), nullable=True)

    signal = relationship("Signal", back_populates="orders")


class Position(Base):
    """Mirrors Alpaca positions but tags each with strategy + open metadata.

    Alpaca's API doesn't preserve which strategy opened a position, so we
    track that ourselves. Closed positions stay in the table for the audit log.
    """
    __tablename__ = "positions"
    id = Column(Integer, primary_key=True)
    symbol = Column(String(16), nullable=False, index=True)
    strategy_name = Column(String(64), nullable=False, index=True)
    side = Column(String(8), nullable=False)
    qty = Column(Integer, nullable=False)
    avg_entry_price = Column(Float, nullable=False)
    stop_loss = Column(Float, nullable=True)
    take_profit = Column(Float, nullable=True)
    opened_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    closed_at = Column(DateTime, nullable=True)
    exit_price = Column(Float, nullable=True)
    realized_pnl = Column(Float, nullable=True)
    exit_reason = Column(String(32), nullable=True)
    is_open = Column(Boolean, default=True, nullable=False, index=True)

    __table_args__ = (
        # While a position is open, only one row per symbol can be open.
        # We enforce in app code rather than partial unique index for SQLite portability.
        Index("ix_position_open", "symbol", "is_open"),
    )


class Alert(Base):
    __tablename__ = "alerts"
    id = Column(Integer, primary_key=True)
    sent_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    channel = Column(String(16), nullable=False)         # telegram | log
    kind = Column(String(32), nullable=False)            # signal | order_submitted | error | summary
    body = Column(String(2048), nullable=False)
    delivered = Column(Boolean, default=False, nullable=False)
    error = Column(String(512), nullable=True)
