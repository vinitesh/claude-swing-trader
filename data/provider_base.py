"""Abstract DataProvider: returns daily OHLCV bars for a symbol."""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date

import pandas as pd


REQUIRED_COLUMNS = ["open", "high", "low", "close", "volume"]


class DataProvider(ABC):
    """Returns daily bars indexed by datetime, with REQUIRED_COLUMNS."""

    name: str = "base"

    @abstractmethod
    def get_bars(
        self,
        symbol: str,
        start: str | date,
        end: str | date,
    ) -> pd.DataFrame:
        """Return a DataFrame indexed by date with REQUIRED_COLUMNS.

        The DataFrame's `attrs["symbol"]` should be set to the symbol so
        downstream code can identify it.
        """
        ...

    @staticmethod
    def _validate(df: pd.DataFrame, symbol: str) -> pd.DataFrame:
        if df is None or df.empty:
            raise ValueError(f"No data returned for {symbol}")
        # Normalize column names
        df = df.rename(columns={c: c.lower() for c in df.columns})
        missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
        if missing:
            raise ValueError(f"{symbol}: missing columns {missing}; got {df.columns.tolist()}")
        df = df[REQUIRED_COLUMNS].dropna()
        df.attrs["symbol"] = symbol
        return df
