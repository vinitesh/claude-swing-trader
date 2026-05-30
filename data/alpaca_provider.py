"""Alpaca historical data provider."""

from __future__ import annotations

from datetime import date, datetime

import pandas as pd
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

from data.provider_base import DataProvider


class AlpacaProvider(DataProvider):
    name = "alpaca"

    def __init__(self, api_key: str, secret_key: str, feed: str = "iex"):
        self.client = StockHistoricalDataClient(api_key, secret_key)
        self.feed = feed

    def get_bars(
        self,
        symbol: str,
        start: str | date,
        end: str | date,
    ) -> pd.DataFrame:
        req = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=TimeFrame.Day,
            start=_to_dt(start),
            end=_to_dt(end),
            feed=self.feed,
        )
        resp = self.client.get_stock_bars(req)
        df = resp.df
        if df is None or df.empty:
            raise ValueError(f"Alpaca returned no data for {symbol} {start}..{end}")
        # Multi-symbol response: index is (symbol, timestamp). Drop symbol level.
        if isinstance(df.index, pd.MultiIndex):
            df = df.xs(symbol, level=0)
        return self._validate(df, symbol)


def _to_dt(d: str | date) -> datetime:
    if isinstance(d, datetime):
        return d
    if isinstance(d, date):
        return datetime(d.year, d.month, d.day)
    return datetime.fromisoformat(str(d))
