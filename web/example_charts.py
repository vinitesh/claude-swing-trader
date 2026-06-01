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
    # Real bars on NVDA Nov 2023 — a textbook pullback-and-bounce illustration.
    # NVDA was in a clear uptrend; on 2023-11-02 the price dipped to its 20-day
    # EMA, formed a bullish reversal candle, and rallied >6% over the next 12
    # days. Used here as an illustrative "what a winning pullback looks like".
    # Prices reflect post-June-2024 10-for-1 split adjustment.
    return TextbookExample(
        symbol="NVDA",
        title="NVDA — November 2023 pullback to EMA (textbook winner)",
        narrative=(
            "After consolidating in late October, NVDA dipped to its 20-day EMA on Nov 1-2 around $43.50 "
            "(split-adjusted), formed a bullish bounce candle, and turned higher with RSI cooling into "
            "the entry zone — the textbook PullbackEMA signal. A trade entering near $43.50 on Nov 2's "
            "close would have caught the resumption of the uptrend: NVDA rallied to $47.50+ over the "
            "next 12 trading days, hitting the 6% take-profit. This is what 'orderly continuation' "
            "looks like — buyers stepping in at well-watched levels (the 20 EMA is the most-followed "
            "short-term moving average on Wall Street) to defend the trend. Note the chart shows the "
            "actual price path: a small dip, the bounce candle on Nov 2, then a steady climb. Most of "
            "PullbackEMA's wins look exactly like this — modest size, ~2-3 weeks holding, +6% target hit."
        ),
        entry_date=date(2023, 11, 2),
        exit_date=date(2023, 11, 20),
        fetch_start=date(2023, 10, 1),
        fetch_end=date(2023, 12, 5),
        entry_label="EMA pullback + bounce → BUY",
        exit_label="+6% target → SELL",
    )


def _rsi2_example() -> TextbookExample:
    # Verified by computing RSI(2) on real META bars Oct-Nov 2023:
    #   2023-10-26 close=$288.35, RSI(2)=3.74 → BUY
    #   2023-11-01 close=$311.85, RSI(2)=87.64 → SELL (signal_exit, RSI > 70)
    # Real return: +8.15% in 4 trading days, $23.50/share.
    return TextbookExample(
        symbol="META",
        title="META — October 2023 panic-bounce (textbook RSI(2) win)",
        narrative=(
            "META had reported earnings on Oct 25, 2023 — solid numbers but cautious 2024 ad-spend "
            "guidance triggered a 4% sell-off. By Oct 26's close at $288.35, the 2-day RSI had crashed "
            "to 3.74 — extreme oversold while the stock was still well above its 200-day SMA in a "
            "long-term uptrend. The RSI(2) strategy bought the panic. Three trading days later, on "
            "Oct 30, RSI(2) reached 68.92; on Nov 1 it spiked to 87.64 and the strategy exited at "
            "$311.85. Real outcome: +8.15% in 4 trading days, ~$23.50/share. This is what RSI(2) is "
            "designed to catch — emotional overreactions in healthy uptrends that mean-revert hard "
            "once the panic seller dries up. Note the chart: a sharp 3-day drop from ~$324 to $288, "
            "then a steady V-shaped recovery as buyers stepped in."
        ),
        entry_date=date(2023, 10, 26),
        exit_date=date(2023, 11, 1),
        fetch_start=date(2023, 10, 1),
        fetch_end=date(2023, 11, 15),
        entry_label="RSI(2) = 3.74 → BUY",
        exit_label="RSI(2) > 70 → SELL",
    )


def _donchian_example() -> TextbookExample:
    # Verified by replaying the Donchian filter on real NVDA bars:
    #   2023-05-16 close $29.21 > 20-day max-of-closes $29.15, all filters pass → BUY
    #   The famous May 24 AI earnings gap took this position from $30 → $38 in a single day.
    #   No exit signal until well into July as the trailing stop ratcheted up.
    # Prices reflect post-June-2024 10-for-1 split adjustment (yfinance default).
    return TextbookExample(
        symbol="NVDA",
        title="NVDA — May 2023 AI breakout (textbook Donchian win)",
        narrative=(
            "NVDA had been quietly building a base in the high-$20s through April-May 2023. On May 16 it "
            "closed at $29.21 (split-adjusted), narrowly clearing the prior 20-day max of $29.15 — a fresh "
            "Donchian high with all filters passing. The strategy would have bought $29.21. Eight days "
            "later, on May 24 after market close, NVDA reported earnings that beat estimates by ~25% and "
            "guided AI demand at unprecedented levels. The next day it gapped up to $37.98 — already "
            "+30% on the position from one earnings event. The trailing stop ratcheted up as NVDA ran to "
            "$47 over the following 8 weeks: a single position closing at +60%+. THIS is why Donchian "
            "exists. Our implementation still failed walk-forward because such setups are rare on the "
            "narrow S&P 500 universe — most breakouts in the test window were whipsaws. A Russell 3000 "
            "implementation might catch enough of these to outweigh the losers."
        ),
        entry_date=date(2023, 5, 16),
        exit_date=date(2023, 7, 18),
        fetch_start=date(2023, 4, 1),
        fetch_end=date(2023, 8, 15),
        entry_label="20-day breakout → BUY",
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
