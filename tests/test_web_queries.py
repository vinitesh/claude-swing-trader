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


# ----------------- strategy_allocations -----------------
def test_allocations_invested_and_pct(session):
    # rsi2 holds 10 @ 100 = $1000 invested against a $33k cap.
    _seed(session, "AAPL")  # rsi2, 10 @ 100
    caps = {"rsi2": 33000.0, "donchian": 33000.0, "pullback_ema": 33000.0}
    rows = {a.strategy_name: a for a in q.strategy_allocations(session, caps=caps)}
    assert set(rows) == {"rsi2", "donchian", "pullback_ema"}
    assert rows["rsi2"].invested == 1000.0
    assert rows["rsi2"].n_open == 1
    assert rows["rsi2"].pct_used == pytest.approx(1000.0 / 33000.0)
    # A capped-but-flat strategy still appears with $0 invested, 0% used.
    assert rows["donchian"].invested == 0.0
    assert rows["donchian"].n_open == 0
    assert rows["donchian"].pct_used == 0.0


def test_allocations_market_value_from_marks(session):
    _seed(session, "AAPL")  # rsi2, 10 @ 100
    caps = {"rsi2": 33000.0}
    marks = {"AAPL": {"current_price": 110.0, "market_value": 1100.0,
                      "unrealized_pl": 100.0, "unrealized_plpc": 0.10}}
    rows = {a.strategy_name: a for a in q.strategy_allocations(session, caps=caps, marks=marks)}
    assert rows["rsi2"].market_value == 1100.0
    # invested (cost) stays at entry basis, distinct from live market value
    assert rows["rsi2"].invested == 1000.0


def test_allocations_market_value_none_without_marks(session):
    _seed(session, "AAPL")
    caps = {"rsi2": 33000.0}
    rows = {a.strategy_name: a for a in q.strategy_allocations(session, caps=caps)}
    assert rows["rsi2"].market_value is None  # marks unavailable → None, no lie


def test_allocations_uncapped_strategy_pct_none(session):
    _seed(session, "AAPL")
    caps = {"rsi2": 0.0}  # 0 = uncapped
    rows = {a.strategy_name: a for a in q.strategy_allocations(session, caps=caps)}
    assert rows["rsi2"].cap_usd == 0.0
    assert rows["rsi2"].pct_used is None  # can't compute % of an uncapped strategy


def test_allocations_position_in_unknown_strategy_still_shown(session):
    # Defensive: an open position whose strategy isn't in caps must still appear.
    _seed(session, "AAPL")  # rsi2
    caps = {"donchian": 33000.0}  # rsi2 absent
    rows = {a.strategy_name: a for a in q.strategy_allocations(session, caps=caps)}
    assert "rsi2" in rows and rows["rsi2"].invested == 1000.0
    assert rows["rsi2"].cap_usd == 0.0
