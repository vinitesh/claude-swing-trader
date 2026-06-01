"""Earnings calendar — avoid trading right before earnings announcements.

Why: end-of-day strategies set entries based on the day's close, but earnings
announcements happen AFTER the close. By next-day open, the stock has gapped
on news the strategy never saw. Stops fire near the gap, R:R is broken,
sometimes the position is just toast. Connors et al. recommend skipping any
entry within ~3 trading days of an earnings event for mean-reversion
strategies; pullback strategies benefit too because earnings disrupt
"orderly continuation".

Data source: yfinance. We use Ticker.get_earnings_dates() which returns a
DataFrame indexed by datetime with confirmed + estimated future dates. We
cache aggressively because the lookup is slow (one HTTP call per symbol)
and the data only changes when companies announce new earnings dates
(weekly cadence at most).

Cache strategy:
    data_cache/earnings/<symbol>.parquet  →  DataFrame of upcoming dates
    Refreshed if older than EARNINGS_CACHE_TTL_HOURS (default 7 days).

Why 7 days? Companies announce their next earnings date 2-4 weeks in
advance at minimum (often 6-8). Our entry filter window is 3 days. So a
7-day TTL gives us ~2 weeks of safety margin before any reschedule could
slip through, while cutting yfinance calls ~85% vs daily refresh. The
nightly pre-warm cron job (`swingbot warm-earnings`) keeps the cache
warm in practice so trading runs almost never trigger live fetches.

Failure mode: if yfinance returns no data for a symbol, we treat it as
"no known earnings" — signal proceeds. This is intentional: failing closed
would block trading on every symbol the moment yfinance hiccups. The
trade-off is that we may occasionally trade through an earnings event we
couldn't fetch; bracket-order stop limits the damage.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

log = logging.getLogger(__name__)

CACHE_DIR = Path(__file__).resolve().parent.parent / "data_cache" / "earnings"
EARNINGS_CACHE_TTL_HOURS = 24 * 7    # 7 days; see module docstring


class EarningsCalendar:
    """Per-symbol next-earnings-date lookup with on-disk cache.

    Constructed once per backtest / per live run; carries a per-symbol
    cache so repeated `next_earnings(...)` calls within one run are free.
    """

    def __init__(self, ttl_hours: int = EARNINGS_CACHE_TTL_HOURS):
        self.ttl_hours = ttl_hours
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        self._memory: dict[str, date | None] = {}

    # ----------------- public API -----------------
    def next_earnings_date(self, symbol: str) -> date | None:
        """Return the next future earnings date for a symbol, or None if
        unknown (yfinance returned nothing or all known dates are past).
        """
        if symbol in self._memory:
            return self._memory[symbol]

        df = self._load_or_fetch(symbol)
        if df is None or df.empty:
            self._memory[symbol] = None
            return None

        today = pd.Timestamp(date.today()).tz_localize(None)
        # Normalize index to tz-naive so comparison works
        idx = df.index
        try:
            idx = idx.tz_localize(None)
        except (TypeError, AttributeError):
            pass  # already tz-naive
        future = idx[idx >= today]
        if len(future) == 0:
            self._memory[symbol] = None
            return None
        next_e = future.min().date()
        self._memory[symbol] = next_e
        return next_e

    def has_earnings_within(
        self, symbol: str, days: int, today: date | None = None
    ) -> bool:
        """True if symbol has earnings within `days` calendar days from `today`."""
        next_e = self.next_earnings_date(symbol)
        if next_e is None:
            return False
        ref = today or date.today()
        return 0 <= (next_e - ref).days <= days

    def refresh(
        self,
        symbols: list[str],
        force: bool = False,
        sleep_secs: float = 0.5,
    ) -> dict[str, str]:
        """Refresh the cache for many symbols in one batch.

        Used by the nightly pre-warm job. Walks symbols sequentially with a
        small sleep between calls to stay polite with yfinance — total time
        scales linearly with symbol count (~10 min for S&P 500).

        Args:
            symbols: ticker list to refresh.
            force: if True, refetch even if cache is fresh; otherwise honor TTL.
            sleep_secs: delay between calls to spread load.

        Returns:
            Dict of symbol → status: "fresh", "refreshed", or "failed".
        """
        import time
        statuses: dict[str, str] = {}
        for sym in symbols:
            path = CACHE_DIR / f"{sym}.parquet"
            if not force and path.exists():
                age_hours = (datetime.now().timestamp() - path.stat().st_mtime) / 3600
                if age_hours < self.ttl_hours:
                    statuses[sym] = "fresh"
                    continue
            # invalidate in-memory cache so subsequent reads pick up the new data
            self._memory.pop(sym, None)
            df = self._fetch(sym, path)
            statuses[sym] = "refreshed" if df is not None else "failed"
            if sleep_secs > 0:
                time.sleep(sleep_secs)
        return statuses

    # ----------------- internal -----------------
    def _load_or_fetch(self, symbol: str) -> pd.DataFrame | None:
        path = CACHE_DIR / f"{symbol}.parquet"
        if path.exists():
            age_hours = (datetime.now().timestamp() - path.stat().st_mtime) / 3600
            if age_hours < self.ttl_hours:
                try:
                    return pd.read_parquet(path)
                except Exception as e:
                    log.warning("Earnings cache for %s unreadable, re-fetching: %s", symbol, e)
        return self._fetch(symbol, path)

    @staticmethod
    def _fetch(symbol: str, path: Path) -> pd.DataFrame | None:
        """Pull future earnings dates from yfinance and cache.

        Lazy import yfinance so the data layer doesn't pay the cost when
        callers don't need earnings (e.g. unit tests with mocked calendar).
        """
        try:
            import yfinance as yf  # noqa: WPS433
            t = yf.Ticker(symbol)
            df = t.get_earnings_dates(limit=8)  # next ~8 quarters
            if df is None or df.empty:
                # write empty marker so we don't refetch repeatedly
                empty = pd.DataFrame()
                empty.to_parquet(path)
                return empty
            df.to_parquet(path)
            return df
        except Exception as e:
            log.warning("yfinance earnings fetch failed for %s: %s", symbol, e)
            # Don't cache failures — try again on next run
            return None
