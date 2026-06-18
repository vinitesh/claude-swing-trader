"""Tests for shared exit-decision logic (core.exits).

These guard the fix that gave the live runner real exit handling: signal exit
(RSI>70), time stop, and trailing-stop ratchet — the same decisions the
backtester uses, so live trades the way it was validated.
"""

from __future__ import annotations

from datetime import date, datetime

import pandas as pd

from core.exits import compute_trailing_stop, indicator_exit_reason
from core.signal import Position


def _pos(opened_at, stop_loss=100.0, side="long") -> Position:
    return Position(
        symbol="TST", qty=10, avg_entry_price=110.0, side=side,
        strategy_name="x", opened_at=opened_at, stop_loss=stop_loss,
        take_profit=130.0,
    )


class _Strat:
    """Minimal strategy double with configurable exit behavior."""
    def __init__(self, *, trail=None, exit_signal=False, time_stop=5):
        self._trail = trail
        self._exit_signal = exit_signal
        self._time_stop = time_stop

    def update_trailing_stop(self, position, df):
        return self._trail

    def should_exit_signal(self, position, df):
        return self._exit_signal

    def time_stop_days(self):
        return self._time_stop


WINDOW = pd.DataFrame({"close": [100, 101, 102]})


# ---------- trailing stop ----------
def test_trailing_stop_ratchets_up():
    s = _Strat(trail=105.0)  # candidate above current 100 → adopt
    assert compute_trailing_stop(s, _pos(datetime(2026, 6, 1), stop_loss=100.0), WINDOW) == 105.0


def test_trailing_stop_never_lowers():
    s = _Strat(trail=95.0)  # candidate below current 100 → reject
    assert compute_trailing_stop(s, _pos(datetime(2026, 6, 1), stop_loss=100.0), WINDOW) is None


def test_trailing_stop_none_when_strategy_returns_none():
    s = _Strat(trail=None)
    assert compute_trailing_stop(s, _pos(datetime(2026, 6, 1)), WINDOW) is None


def test_trailing_stop_skips_short_side():
    s = _Strat(trail=105.0)
    assert compute_trailing_stop(s, _pos(datetime(2026, 6, 1), side="short"), WINDOW) is None


# ---------- signal exit ----------
def test_signal_exit_fires_first():
    s = _Strat(exit_signal=True, time_stop=5)
    # Even though only 1 day held (< time stop), signal exit wins.
    assert indicator_exit_reason(s, _pos(datetime(2026, 6, 17)), WINDOW, date(2026, 6, 18)) == "signal_exit"


def test_no_exit_when_nothing_triggers():
    s = _Strat(exit_signal=False, time_stop=5)
    assert indicator_exit_reason(s, _pos(datetime(2026, 6, 17)), WINDOW, date(2026, 6, 18)) is None


# ---------- time stop ----------
def test_time_stop_fires_at_threshold():
    s = _Strat(exit_signal=False, time_stop=5)
    # opened 5 days before today → held == 5 >= 5
    assert indicator_exit_reason(s, _pos(datetime(2026, 6, 13)), WINDOW, date(2026, 6, 18)) == "time_stop"


def test_time_stop_not_yet():
    s = _Strat(exit_signal=False, time_stop=5)
    assert indicator_exit_reason(s, _pos(datetime(2026, 6, 15)), WINDOW, date(2026, 6, 18)) is None


def test_signal_exit_takes_precedence_over_time_stop():
    s = _Strat(exit_signal=True, time_stop=5)
    # both would fire; signal_exit must win
    assert indicator_exit_reason(s, _pos(datetime(2026, 6, 1)), WINDOW, date(2026, 6, 18)) == "signal_exit"


def test_datetime_today_accepted():
    s = _Strat(exit_signal=False, time_stop=5)
    assert indicator_exit_reason(s, _pos(datetime(2026, 6, 13)), WINDOW, datetime(2026, 6, 18, 16, 5)) == "time_stop"
