"""Regression: get_last_exit_fill must not crash on tz-naive vs tz-aware compare.

Alpaca returns tz-AWARE filled_at; the DB stores tz-NAIVE opened_at. Comparing
them raised TypeError, which got swallowed → every real exit was recorded as
no_exit_fill at $0 P&L. This locks in the fix.
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
from types import SimpleNamespace

from execution.alpaca_broker import AlpacaBroker, _as_naive_utc


class _FakeClient:
    def __init__(self, orders):
        self._orders = orders

    def get_orders(self, filter=None):
        return self._orders


def _broker_with(orders):
    b = AlpacaBroker.__new__(AlpacaBroker)   # bypass __init__/network
    b.client = _FakeClient(orders)
    b.paper = True
    return b


def _filled_sell(price, filled_at):
    return SimpleNamespace(
        filled_qty="10", filled_avg_price=str(price),
        filled_at=filled_at, updated_at=filled_at, symbol="AAPL",
    )


def test_as_naive_utc_strips_tz():
    aware = datetime(2026, 6, 22, 16, 3, tzinfo=timezone.utc)
    naive = _as_naive_utc(aware)
    assert naive.tzinfo is None
    assert naive == datetime(2026, 6, 22, 16, 3)
    assert _as_naive_utc(None) is None
    # already-naive passes through
    n = datetime(2026, 6, 22, 16, 3)
    assert _as_naive_utc(n) == n


def test_aware_fill_vs_naive_opened_after_does_not_crash():
    # The exact production scenario: tz-aware Alpaca fill, tz-naive DB opened_at.
    fill_at = datetime(2026, 6, 22, 16, 3, tzinfo=timezone.utc)   # aware
    opened = datetime(2026, 6, 18, 20, 5)                          # naive (DB)
    broker = _broker_with([_filled_sell(69.09, fill_at)])
    res = broker.get_last_exit_fill("AAPL", opened_after=opened)
    assert res is not None, "real fill must be returned, not discarded"
    price, when = res
    assert price == 69.09
    assert when.tzinfo is None  # normalized


def test_stale_fill_before_open_is_skipped():
    # A sell that filled BEFORE this position opened is a prior trade → skip.
    old_fill = datetime(2026, 6, 1, 15, 0, tzinfo=timezone.utc)
    opened = datetime(2026, 6, 18, 20, 5)
    broker = _broker_with([_filled_sell(50.0, old_fill)])
    assert broker.get_last_exit_fill("AAPL", opened_after=opened) is None


def test_no_opened_after_returns_latest_fill():
    fill_at = datetime(2026, 6, 22, 16, 3, tzinfo=timezone.utc)
    broker = _broker_with([_filled_sell(69.09, fill_at)])
    res = broker.get_last_exit_fill("AAPL", opened_after=None)
    assert res is not None and res[0] == 69.09


# ---------- backfill window/qty disambiguation ----------
def _sell(price, filled_at, qty):
    return SimpleNamespace(
        filled_qty=str(qty), filled_avg_price=str(price),
        filled_at=filled_at, updated_at=filled_at, symbol="AAPL",
    )


def test_window_filters_by_time_bounds():
    inside = datetime(2026, 6, 19, 15, 0, tzinfo=timezone.utc)
    after = datetime(2026, 6, 25, 15, 0, tzinfo=timezone.utc)
    broker = _broker_with([_sell(100.0, after, 10), _sell(95.0, inside, 10)])
    fills = broker.find_exit_fills_in_window(
        "AAPL", datetime(2026, 6, 18), datetime(2026, 6, 20), qty=10,
    )
    assert len(fills) == 1 and fills[0][0] == 95.0


def test_qty_match_disambiguates_retrades():
    t = datetime(2026, 6, 19, 15, 0, tzinfo=timezone.utc)
    # two sells in window, different sizes; only qty=10 should match
    broker = _broker_with([_sell(100.0, t, 5), _sell(95.0, t, 10)])
    fills = broker.find_exit_fills_in_window(
        "AAPL", datetime(2026, 6, 18), datetime(2026, 6, 20), qty=10,
    )
    assert len(fills) == 1 and fills[0][0] == 95.0 and fills[0][2] == 10


def test_multiple_same_qty_sells_flagged_as_ambiguous():
    t1 = datetime(2026, 6, 19, 15, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 6, 19, 16, 0, tzinfo=timezone.utc)
    broker = _broker_with([_sell(101.0, t2, 10), _sell(99.0, t1, 10)])
    fills = broker.find_exit_fills_in_window(
        "AAPL", datetime(2026, 6, 18), datetime(2026, 6, 20), qty=10,
    )
    assert len(fills) == 2  # caller treats >1 as ambiguous → skip
