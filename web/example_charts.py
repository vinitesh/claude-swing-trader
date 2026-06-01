"""Fetch + cache historical price bars for the strategy textbook examples.

Each strategy's explainer page shows a hand-curated historical trade
("textbook example") with an annotated price chart. We fetch the relevant
window from yfinance once, cache as parquet under data_cache/textbook/,
and serve from cache on subsequent loads.

Failure-tolerant: if yfinance is down or the symbol is unavailable, the
page just omits the chart — the rest of the explainer renders normally.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

import pandas as pd

log = logging.getLogger(__name__)

CACHE_DIR = Path(__file__).resolve().parent.parent / "data_cache" / "textbook"
CACHE_TTL_DAYS = 30  # historical bars don't change; refresh monthly is plenty


@dataclass
class TextbookExample:
    """A hand-curated historical trade that perfectly demonstrates a setup."""
    symbol: str
    title: str                      # short name for the chart header
    narrative: str                  # 2-4 sentences explaining the trade
    entry_date: date                # the bar where the strategy would have entered
    exit_date: date                 # the bar where it would have exited
    fetch_start: date               # bars to load (typically 30 days before entry)
    fetch_end: date                 # bars to load (typically 10 days after exit)
    entry_label: str = "Entry"      # text for entry annotation
    exit_label: str = "Exit"        # text for exit annotation
    bars: list[dict] = field(default_factory=list)  # populated by fetch_bars()
    error: str | None = None        # populated if fetch fails


# ----------------- public API -----------------
def get_textbook_example(strategy_name: str) -> TextbookExample | None:
    """Return the textbook example for a strategy, with bars hydrated."""
    base = _REGISTRY.get(strategy_name)
    if base is None:
        return None
    _hydrate_bars(base)
    return base


# ----------------- the curated examples -----------------
def _pullback_ema_example() -> TextbookExample:
    return TextbookExample(
        symbol="NVDA",
        title="NVDA — October 2023 pullback to 20 EMA",
        narrative=(
            "After NVDA's parabolic AI rally through summer 2023, the stock cooled off in early October "
            "and pulled back to its 20-day EMA on Oct 17-18. The pullback brought price within 1% of the "
            "EMA while RSI(14) cooled into the 40-50 zone — a textbook PullbackEMA setup. The strategy "
            "would have bought on Oct 19's bullish bounce candle. Over the next three weeks NVDA rallied "
            "another ~10%, hitting the 6% take-profit. This is what 'orderly continuation' looks like "
            "in the wild. (Chart shows split-adjusted prices after NVDA's June 2024 10-for-1 split.)"
        ),
        entry_date=date(2023, 10, 19),
        exit_date=date(2023, 11, 13),
        fetch_start=date(2023, 9, 15),
        fetch_end=date(2023, 11, 24),
        entry_label="EMA pullback + bounce → BUY",
        exit_label="+6% target → SELL",
    )


def _rsi2_example() -> TextbookExample:
    return TextbookExample(
        symbol="WMT",
        title="WMT — August 2023 oversold bounce",
        narrative=(
            "Walmart had been quietly trending up for months when an earnings disappointment on Aug 17 "
            "punished the stock for two days, dropping it ~5% on heavy volume. By Aug 21 the 2-day RSI "
            "had crashed to single digits — extreme oversold inside a clear uptrend (close still well "
            "above the 200-day SMA). The RSI(2) strategy would have bought the panic at ~$155. Within "
            "three trading days RSI(2) ripped back above 70 as buyers stepped in, and the strategy "
            "would have exited near $159 — a small +2.5% win, but exactly the high-frequency setup that "
            "produces RSI(2)'s ~70% win rate over hundreds of trades per year."
        ),
        entry_date=date(2023, 8, 21),
        exit_date=date(2023, 8, 24),
        fetch_start=date(2023, 7, 15),
        fetch_end=date(2023, 9, 5),
        entry_label="RSI(2) < 5 → BUY",
        exit_label="RSI(2) > 70 → SELL",
    )


def _donchian_example() -> TextbookExample:
    return TextbookExample(
        symbol="NVDA",
        title="NVDA — May 2023 AI breakout (the trade we missed)",
        narrative=(
            "On May 24, 2023 NVDA reported earnings that beat estimates by ~25% and guided AI demand at "
            "unprecedented levels. The next day it gapped up ~25% and broke out of a multi-month base, far "
            "exceeding any 20-day high. A Donchian breakout strategy would have bought at the open and "
            "ridden the trailing stop for months — NVDA put on another ~35% over the next 8 weeks on a "
            "single position. THIS is why Donchian exists. Our version failed the walk-forward sweep "
            "because the S&P 500 universe is too narrow to surface enough setups like this. A Russell "
            "3000 implementation might catch them. (Chart shows split-adjusted prices after NVDA's June "
            "2024 10-for-1 split.)"
        ),
        entry_date=date(2023, 5, 25),
        exit_date=date(2023, 7, 18),
        fetch_start=date(2023, 4, 1),
        fetch_end=date(2023, 8, 15),
        entry_label="20-day high breakout → BUY",
        exit_label="Trailing stop → SELL",
    )


_REGISTRY: dict[str, TextbookExample] = {
    "pullback_ema": _pullback_ema_example(),
    "rsi2": _rsi2_example(),
    "donchian": _donchian_example(),
}


# ----------------- bar fetcher -----------------
def _hydrate_bars(ex: TextbookExample) -> None:
    """Populate `bars` on an example by reading cache or fetching from yfinance."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path = CACHE_DIR / f"{ex.symbol}_{ex.fetch_start}_{ex.fetch_end}.parquet"

    df = None
    if cache_path.exists():
        age_days = (datetime.now().timestamp() - cache_path.stat().st_mtime) / 86400
        if age_days < CACHE_TTL_DAYS:
            try:
                df = pd.read_parquet(cache_path)
            except Exception as e:
                log.warning("textbook cache for %s unreadable: %s", ex.symbol, e)

    if df is None:
        df = _fetch_yf(ex.symbol, ex.fetch_start, ex.fetch_end)
        if df is not None:
            try:
                df.to_parquet(cache_path)
            except Exception as e:
                log.warning("could not write textbook cache: %s", e)

    if df is None or df.empty:
        ex.error = "Price data unavailable — chart will be skipped."
        return

    # Format for Chart.js: list of {date, close}
    out = []
    for ts, row in df.iterrows():
        out.append({
            "date": ts.strftime("%Y-%m-%d") if hasattr(ts, "strftime") else str(ts),
            "close": float(row["close"]),
        })
    ex.bars = out


def _fetch_yf(symbol: str, start: date, end: date) -> pd.DataFrame | None:
    try:
        import yfinance as yf
        # auto_adjust=False so historic stock-split prices match the dollar
        # values we cite in the narrative (e.g. NVDA at $440 pre-split).
        # Backtests use auto_adjust=True (continuous series) but explainer
        # charts are about "what people saw at the time".
        df = yf.download(
            symbol,
            start=str(start),
            end=str(end),
            interval="1d",
            progress=False,
            auto_adjust=False,
            threads=False,
        )
        if df is None or df.empty:
            return None
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df.columns = [c.lower() for c in df.columns]
        return df
    except Exception as e:
        log.warning("yfinance fetch failed for %s: %s", symbol, e)
        return None
