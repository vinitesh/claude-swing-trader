"""Tests for web.queries.open_positions mark-to-market enrichment.

Guards the dashboard unrealized-P&L feature: when broker marks are supplied
they populate the row; when missing (broker unreachable) the row degrades to
None → the template renders "—" instead of erroring.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from persistence import repository as repo
from persistence.models import Base
from web import queries as q


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


def _seed(session, symbol="AAPL"):
    repo.open_position(
        session, symbol=symbol, strategy_name="rsi2", side="long",
        qty=10, avg_entry_price=100.0, stop_loss=97.0, take_profit=120.0,
    )


def test_marks_enrich_row(session):
    _seed(session, "AAPL")
    marks = {"AAPL": {
        "current_price": 110.0, "market_value": 1100.0,
        "unrealized_pl": 100.0, "unrealized_plpc": 0.10,
    }}
    rows = q.open_positions(session, marks=marks)
    assert len(rows) == 1
    r = rows[0]
    assert r.current_price == 110.0
    assert r.unrealized_pl == 100.0
    assert r.unrealized_plpc == 0.10


def test_missing_marks_degrade_to_none(session):
    _seed(session, "AAPL")
    # No marks at all (broker unreachable) → unrealized fields None.
    rows = q.open_positions(session)
    assert rows[0].current_price is None
    assert rows[0].unrealized_pl is None


def test_partial_marks_only_some_symbols(session):
    _seed(session, "AAPL")
    _seed(session, "MSFT")
    marks = {"AAPL": {"current_price": 110.0, "market_value": 1100.0,
                      "unrealized_pl": 100.0, "unrealized_plpc": 0.10}}
    rows = {r.symbol: r for r in q.open_positions(session, marks=marks)}
    assert rows["AAPL"].unrealized_pl == 100.0
    assert rows["MSFT"].unrealized_pl is None  # not in marks → None, no crash


def test_negative_unrealized(session):
    _seed(session, "AAPL")
    marks = {"AAPL": {"current_price": 90.0, "market_value": 900.0,
                      "unrealized_pl": -100.0, "unrealized_plpc": -0.10}}
    rows = q.open_positions(session, marks=marks)
    assert rows[0].unrealized_pl == -100.0
