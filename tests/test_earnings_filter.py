"""Earnings-filter behavior tests.

We use a fake EarningsCalendar to drive deterministic outcomes — never hit
yfinance from tests. The fake exposes the same `has_earnings_within(symbol,
days, today=...)` interface the strategies call.

Three properties:
  1. With earnings within window → strategy returns None (signal blocked)
  2. With earnings outside window → strategy returns the signal it would
     have produced without the filter
  3. avoid_earnings_within_days = 0 → filter is fully bypassed even if a
     calendar is attached
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from core.config import load_strategy_config
from strategies.pullback_ema import PullbackEMA
from strategies.rsi2 import RSI2


class FakeEarnings:
    """Drop-in replacement for EarningsCalendar with deterministic responses."""

    def __init__(self, dates: dict[str, date | None]):
        self.dates = dates

    def has_earnings_within(self, symbol: str, days: int, today: date | None = None) -> bool:
        d = self.dates.get(symbol)
        if d is None:
            return False
        ref = today or date.today()
        return 0 <= (d - ref).days <= days


def _force_signal_df(symbol: str = "AAPL") -> pd.DataFrame:
    """Build a DataFrame that — given default PullbackEMA params — would
    produce a signal: clear uptrend, RSI in zone, bullish bounce candle near
    the EMA. We construct synthetic indicators directly so we don't depend on
    pandas-ta producing the right shape; the strategy doesn't recompute them
    when they're already in the DataFrame.
    """
    n = 250
    idx = pd.date_range("2024-01-01", periods=n, freq="B")
    # Steady uptrend, today closes near ema_fast (1.5% proximity), bullish
    df = pd.DataFrame({
        "open":  [100.0] * (n - 1) + [99.5],
        "high":  [102.0] * (n - 1) + [101.0],
        "low":   [98.0]  * (n - 1) + [99.6],
        "close": [101.0] * (n - 1) + [100.5],
        "volume": [1_000_000] * n,
        "ema_fast": [100.5] * n,   # today's low (99.6) within 1% of ema_fast
        "sma_mid":  [99.0]  * n,
        "sma_slow": [97.0]  * n,
        "rsi":      [47.0]  * n,   # in 40-55 default zone
    }, index=idx)
    df.attrs["symbol"] = symbol
    return df


def _make_strategy(cls, **overrides):
    cfg = load_strategy_config(f"{cls.name}.yaml")
    cfg["universe"] = ["AAPL"]
    cfg["require_bull_regime"] = False
    cfg.update(overrides)
    return cls(cfg)


# ---------- PullbackEMA ----------
def test_pullback_blocks_signal_when_earnings_within_window():
    s = _make_strategy(PullbackEMA, avoid_earnings_within_days=3)
    s.earnings_calendar = FakeEarnings({"AAPL": date(2024, 12, 13)})  # today's df ends 2024-12-13ish
    df = _force_signal_df("AAPL")
    # Earnings is within 0 days of df's last index — should block
    s.earnings_calendar.dates["AAPL"] = df.index[-1].date()
    assert s.should_enter(df) is None


def test_pullback_allows_signal_when_earnings_outside_window():
    s = _make_strategy(PullbackEMA, avoid_earnings_within_days=3)
    df = _force_signal_df("AAPL")
    # Earnings 30 days from now — way outside 3-day window
    far_date = (df.index[-1] + pd.Timedelta(days=30)).date()
    s.earnings_calendar = FakeEarnings({"AAPL": far_date})
    sig = s.should_enter(df)
    assert sig is not None, "expected entry signal but got None — base setup may be wrong"
    assert sig.symbol == "AAPL"


def test_pullback_disabled_filter_lets_signal_through_even_with_earnings_today():
    s = _make_strategy(PullbackEMA, avoid_earnings_within_days=0)
    df = _force_signal_df("AAPL")
    s.earnings_calendar = FakeEarnings({"AAPL": df.index[-1].date()})
    sig = s.should_enter(df)
    assert sig is not None


# ---------- RSI(2) ----------
def _force_rsi2_signal_df(symbol: str = "AAPL") -> pd.DataFrame:
    n = 250
    idx = pd.date_range("2024-01-01", periods=n, freq="B")
    df = pd.DataFrame({
        "open":  [100.0] * n,
        "high":  [102.0] * n,
        "low":   [99.0]  * n,
        "close": [101.0] * n,
        "volume":[1_000_000] * n,
        "rsi_short": [50.0] * (n - 1) + [3.0],   # today RSI(2) < 5 → oversold
        "sma_long":  [97.0] * n,                 # close > sma_long
    }, index=idx)
    df.attrs["symbol"] = symbol
    return df


def test_rsi2_blocks_signal_when_earnings_within_window():
    s = _make_strategy(RSI2, avoid_earnings_within_days=3)
    df = _force_rsi2_signal_df("AAPL")
    s.earnings_calendar = FakeEarnings({"AAPL": df.index[-1].date()})
    assert s.should_enter(df) is None


def test_rsi2_allows_signal_when_earnings_far():
    s = _make_strategy(RSI2, avoid_earnings_within_days=3)
    df = _force_rsi2_signal_df("AAPL")
    far_date = (df.index[-1] + pd.Timedelta(days=30)).date()
    s.earnings_calendar = FakeEarnings({"AAPL": far_date})
    sig = s.should_enter(df)
    assert sig is not None


def test_rsi2_no_calendar_attached_means_filter_disabled():
    """Even with avoid_earnings_within_days=3, a missing calendar means
    fail-open: signal proceeds (per design — failing closed would block all
    trading on a single yfinance hiccup)."""
    s = _make_strategy(RSI2, avoid_earnings_within_days=3)
    # explicitly do not set s.earnings_calendar
    df = _force_rsi2_signal_df("AAPL")
    sig = s.should_enter(df)
    assert sig is not None
