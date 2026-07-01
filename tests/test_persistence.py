"""Persistence layer round-trip tests.

Uses an in-memory SQLite DB (separate engine) so we don't pollute trading.db.
"""

from __future__ import annotations

from datetime import date, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.signal import Action, Signal as DomainSignal
from persistence import repository as repo
from persistence.models import Base


@pytest.fixture()
def session():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    s = Session()
    try:
        yield s
        s.commit()
    finally:
        s.close()


def _mk_sig(symbol="AAPL", strat="pullback_ema") -> DomainSignal:
    return DomainSignal(
        symbol=symbol, action=Action.BUY,
        entry_price=100.0, stop_loss=98.0, take_profit=106.0,
        strategy_name=strat, confidence=0.65,
        metadata={"rsi": 47.5},
    )


def test_run_lifecycle(session):
    r = repo.start_run(session, mode="paper", strategies=["pullback_ema"])
    assert r.id is not None
    assert r.finished_at is None
    repo.finish_run(session, r, signals_found=2, orders_submitted=1, orders_skipped=1, orders_rejected=0)
    assert r.finished_at is not None
    assert r.signals_found == 2


def test_signal_idempotency(session):
    r = repo.start_run(session, mode="paper", strategies=["pullback_ema"])
    sig = _mk_sig()
    row1, is_new1 = repo.record_signal(session, r, sig, bar_date=date(2026, 1, 15))
    row2, is_new2 = repo.record_signal(session, r, sig, bar_date=date(2026, 1, 15))
    assert is_new1 is True and row1 is not None
    assert is_new2 is False
    assert row2 is not None and row2.id == row1.id, "duplicate insert should return existing row"


def test_already_acted_today(session):
    r = repo.start_run(session, mode="paper", strategies=["pullback_ema"])
    sig = _mk_sig("MSFT")
    sig_row, _ = repo.record_signal(session, r, sig, bar_date=date(2026, 1, 15))
    assert not repo.signal_already_acted_today(session, "pullback_ema", "MSFT", date(2026, 1, 15))
    repo.record_order_submitted(
        session, sig_row, broker="alpaca", broker_order_id="alp-1",
        symbol="MSFT", side="buy", qty=10,
        entry_price=100.0, stop_loss=98.0, take_profit=106.0,
    )
    assert repo.signal_already_acted_today(session, "pullback_ema", "MSFT", date(2026, 1, 15))
    # Different date → not acted
    assert not repo.signal_already_acted_today(session, "pullback_ema", "MSFT", date(2026, 1, 16))


def test_position_open_close_realized_pnl(session):
    p = repo.open_position(
        session, symbol="NVDA", strategy_name="pullback_ema", side="long",
        qty=50, avg_entry_price=400.0, stop_loss=392.0, take_profit=424.0,
    )
    assert p.is_open is True
    closed = repo.close_position(session, symbol="NVDA", exit_price=424.0, exit_reason="take_profit")
    assert closed is not None
    assert closed.is_open is False
    assert closed.realized_pnl == pytest.approx((424.0 - 400.0) * 50)


def test_close_position_with_duplicate_open_rows_does_not_crash(session):
    # Two open rows on the SAME symbol (strategy re-entered a symbol it held).
    # The old scalar_one_or_none() raised MultipleResultsFound and rolled back
    # the whole sync. Closing must now succeed, one row at a time, oldest first.
    p1 = repo.open_position(
        session, symbol="DOW", strategy_name="rsi2", side="long",
        qty=10, avg_entry_price=29.0, stop_loss=28.0, take_profit=32.0,
    )
    p2 = repo.open_position(
        session, symbol="DOW", strategy_name="rsi2", side="long",
        qty=5, avg_entry_price=30.0, stop_loss=29.0, take_profit=33.0,
    )
    session.flush()

    # Close by explicit id → closes exactly that row, leaves the other open.
    closed = repo.close_position(
        session, symbol="DOW", exit_price=31.0, exit_reason="exit_filled",
        position_id=p2.id,
    )
    assert closed is not None and closed.id == p2.id and closed.is_open is False
    still_open = repo.get_open_positions(session)
    assert [p.id for p in still_open] == [p1.id]

    # Close remaining without id → oldest open row (p1), no crash.
    closed2 = repo.close_position(
        session, symbol="DOW", exit_price=28.5, exit_reason="exit_filled",
    )
    assert closed2 is not None and closed2.id == p1.id
    assert repo.get_open_positions(session) == []


def test_get_open_positions_is_oldest_first(session):
    # sync's duplicate-dedup relies on oldest-first ordering: the oldest row
    # for a symbol receives the real exit fill; later duplicates close flat.
    from datetime import timedelta
    base = datetime(2026, 6, 20, 20, 0, 0)
    younger = repo.open_position(
        session, symbol="DOW", strategy_name="rsi2", side="long",
        qty=5, avg_entry_price=30.0, stop_loss=29.0, take_profit=33.0,
    )
    older = repo.open_position(
        session, symbol="DOW", strategy_name="rsi2", side="long",
        qty=10, avg_entry_price=29.0, stop_loss=28.0, take_profit=32.0,
    )
    # Force opened_at so ordering is by time, not insertion/id.
    older.opened_at = base
    younger.opened_at = base + timedelta(days=2)
    session.flush()
    rows = repo.get_open_positions(session)
    dow = [p for p in rows if p.symbol == "DOW"]
    assert [p.id for p in dow] == [older.id, younger.id]


def test_has_open_position(session):
    assert repo.has_open_position(session, "TSLA") is False
    repo.open_position(
        session, symbol="TSLA", strategy_name="rsi2", side="long",
        qty=1, avg_entry_price=250.0, stop_loss=240.0, take_profit=280.0,
    )
    session.flush()
    assert repo.has_open_position(session, "TSLA") is True
    # After close, no longer counts as open.
    repo.close_position(session, symbol="TSLA", exit_price=260.0, exit_reason="tp")
    assert repo.has_open_position(session, "TSLA") is False


def test_strategy_for_symbol_lookup(session):
    repo.open_position(
        session, symbol="GOOGL", strategy_name="pullback_ema", side="long",
        qty=20, avg_entry_price=140.0, stop_loss=137.0, take_profit=149.0,
    )
    assert repo.get_strategy_for_symbol(session, "GOOGL") == "pullback_ema"
    assert repo.get_strategy_for_symbol(session, "NOT_THERE") is None


def test_order_status_update(session):
    r = repo.start_run(session, mode="paper", strategies=["pullback_ema"])
    sig = _mk_sig("TSLA")
    sig_row, _ = repo.record_signal(session, r, sig, bar_date=date(2026, 1, 15))
    o = repo.record_order_submitted(
        session, sig_row, broker="alpaca", broker_order_id="alp-tsla",
        symbol="TSLA", side="buy", qty=10,
        entry_price=200.0, stop_loss=196.0, take_profit=212.0,
    )
    assert o.status == "submitted"
    repo.update_order_status(session, "alp-tsla", "filled", fill_price=200.05, filled_at=datetime(2026, 1, 15, 14, 31))
    session.refresh(o)
    assert o.status == "filled"
    assert o.fill_price == pytest.approx(200.05)
