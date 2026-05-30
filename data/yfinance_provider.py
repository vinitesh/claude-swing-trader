"""yfinance provider — free, decent quality, daily bars."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd
import yfinance as yf

from data.provider_base import DataProvider


class YFinanceProvider(DataProvider):
    name = "yfinance"

    def __init__(self, cache_dir: str | Path | None = None):
        self.cache_dir = Path(cache_dir) if cache_dir else None
        if self.cache_dir:
            self.cache_dir.mkdir(parents=True, exist_ok=True)

    def get_bars(
        self,
        symbol: str,
        start: str | date,
        end: str | date,
    ) -> pd.DataFrame:
        cache_path = self._cache_path(symbol, start, end)
        if cache_path and cache_path.exists():
            df = pd.read_parquet(cache_path)
        else:
            df = yf.download(
                symbol,
                start=str(start),
                end=str(end),
                interval="1d",
                progress=False,
                auto_adjust=True,
                threads=False,
            )
            if df.empty:
                raise ValueError(f"yfinance returned no data for {symbol} {start}..{end}")
            # yfinance can return a MultiIndex column when multiple tickers — flatten
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            if cache_path:
                df.to_parquet(cache_path)

        return self._validate(df, symbol)

    def _cache_path(self, symbol: str, start, end) -> Path | None:
        if not self.cache_dir:
            return None
        return self.cache_dir / f"{symbol}_{start}_{end}.parquet"
