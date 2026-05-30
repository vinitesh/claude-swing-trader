"""Market-regime filter.

Single-asset (SPY by default) trend filter: are we in a bull regime today?
The check is "close > N-day SMA of close". If False, strategies that opt in
should suppress new long entries — pullbacks in downtrends are not pullbacks,
they're trend continuation against you.

The filter pre-computes a date → bool map at construction so the per-bar
lookup in the hot loop is O(1).
"""

from __future__ import annotations

from datetime import date
from typing import Mapping

import pandas as pd

from data.provider_base import DataProvider


class RegimeFilter:
    def __init__(
        self,
        data_provider: DataProvider,
        symbol: str = "SPY",
        sma_period: int = 200,
        start: str | date | None = None,
        end: str | date | None = None,
    ):
        self.symbol = symbol
        self.sma_period = sma_period
        # Pull enough history for the SMA to be valid on the first backtest day.
        # Caller passes the backtest start; we silently shift the fetch back by
        # ~1.5x the SMA window in calendar days.
        fetch_start = start
        if start is not None:
            ts = pd.Timestamp(start) - pd.Timedelta(days=int(sma_period * 1.5))
            fetch_start = ts.date()
        df = data_provider.get_bars(symbol, start=fetch_start, end=end)
        df["sma"] = df["close"].rolling(sma_period, min_periods=sma_period).mean()
        df["bull"] = df["close"] > df["sma"]
        # Map ts -> bull. NaN sma → False (not enough history → conservative).
        self._by_date: Mapping[pd.Timestamp, bool] = {
            ts: bool(b) and pd.notna(s)
            for ts, b, s in zip(df.index, df["bull"], df["sma"])
        }

    def is_bull(self, ts: pd.Timestamp) -> bool:
        """Is today a bull regime? Returns False on dates we have no data for."""
        return self._by_date.get(pd.Timestamp(ts), False)
