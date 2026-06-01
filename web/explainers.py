"""Strategy explainer page data layer.

For each strategy we provide:
  - hero metadata (tagline, status badge, color)
  - beginner-friendly text sections (what / why / when it works / when it fails)
  - a Mermaid flowchart describing entry decisions
  - one annotated "recent example" trade pulled from the backtest CSV
  - the live YAML parameters

This file owns ALL the human-facing content. The HTML template just renders.
Tests target this module so the copy can be reviewed without firing up a browser.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = PROJECT_ROOT / "config" / "strategies"
BACKTEST_DIR = PROJECT_ROOT / "backtest_results"


@dataclass
class ParamRow:
    label: str
    value: str
    note: str = ""


@dataclass
class ExampleTrade:
    """One representative completed trade pulled from backtest CSV.

    Used to render a real-looking annotated price chart. Picked deterministically
    so the page is stable across reloads.
    """
    symbol: str
    entry_date: str
    exit_date: str
    entry_price: float
    exit_price: float
    qty: int
    pnl: float
    exit_reason: str
    return_pct: float


@dataclass
class StrategyExplainer:
    name: str
    title: str
    tagline: str               # one-line elevator pitch
    status: str                # "paper-ready" | "disabled"
    color: str                 # hex; tints the hero
    test_sharpe: float | None
    test_cagr: float | None
    test_max_dd: float | None

    # Long-form sections, in beginner tone (no jargon assumed)
    what_it_does: str
    why_it_works: str
    when_it_works: list[str]    # bulleted list of "good regimes"
    when_it_fails: list[str]    # bulleted list of "bad regimes"
    glossary: list[tuple[str, str]] = field(default_factory=list)  # (term, definition)

    # Mermaid source — entry decision flowchart
    entry_flowchart: str = ""

    # Live YAML params, formatted for display
    params: list[ParamRow] = field(default_factory=list)

    # One real trade for the example chart (None if backtest CSV missing)
    example: ExampleTrade | None = None


# ----------------- copy for each strategy -----------------
def _pullback_ema() -> StrategyExplainer:
    return StrategyExplainer(
        name="pullback_ema",
        title="Pullback to 20 EMA",
        tagline="Buy quality stocks dipping briefly to their 20-day exponential moving average inside a confirmed uptrend.",
        status="paper-ready",
        color="#0072b2",
        test_sharpe=0.93,
        test_cagr=0.082,
        test_max_dd=-0.106,
        what_it_does=(
            "This strategy looks for stocks that are in clear long-term uptrends but have just had a "
            "small, orderly dip — a 'pullback' — that brings the price right down to its 20-day EMA. "
            "When such a pullback shows a 'bounce candle' (today closes higher than it opened), we buy. "
            "We expect the underlying uptrend to resume within a couple of weeks."
        ),
        why_it_works=(
            "Strong stocks rarely go straight up — they breathe. Profit-takers temporarily lower the price "
            "until new buyers step in around well-watched levels like the 20-day EMA. We're not predicting; "
            "we're piggy-backing on the institutional flows that protect uptrends."
        ),
        when_it_works=[
            "Steady bull-market years (2017, 2021, 2023 H2) — frequent clean pullbacks",
            "Mid-volatility regimes (VIX 12-22) — enough movement to trigger but not chaos",
            "Sector rotations — money cycling between names creates pullback entries",
        ],
        when_it_fails=[
            "Bear markets — every 'pullback' becomes a real downtrend",
            "Vertical rallies (post-COVID Apr-Aug 2020) — no pullbacks happen at all",
            "Choppy ranges — fakeouts trigger stop-losses repeatedly",
        ],
        glossary=[
            ("EMA (Exponential Moving Average)",
             "An average of recent prices that gives more weight to newer days. The 20-day EMA reacts to "
             "the last ~3 trading weeks but smooths daily noise."),
            ("SMA (Simple Moving Average)",
             "Plain average of the last N closing prices. We use SMA(50) and SMA(200) as long-term "
             "trend filters — close > 50 SMA > 200 SMA means 'this stock is up across multiple time scales.'"),
            ("RSI (Relative Strength Index)",
             "An oscillator from 0-100 measuring how 'overbought' or 'oversold' a stock is short-term. "
             "We require it to be in the 40-55 zone — recently dipped but not panic-oversold."),
            ("Bracket order",
             "An order that combines an entry with a stop-loss and take-profit, all submitted at once. "
             "Alpaca monitors all three for us — once entered, exits fire automatically when triggered."),
        ],
        entry_flowchart="""flowchart TD
    A[Daily bar closes] --> B{close > SMA-50<br/>AND SMA-50 > SMA-200?}
    B -- No: not in uptrend --> X[Skip: wait for tomorrow]
    B -- Yes --> C{Today's low<br/>within 1.5% of EMA-20?}
    C -- No: not pulled back enough --> X
    C -- Yes --> D{Bullish candle?<br/>close > open}
    D -- No: still red --> X
    D -- Yes --> E{RSI in 40-55 zone?}
    E -- No: too hot or too cold --> X
    E -- Yes --> F{Earnings within 3 days?}
    F -- Yes --> X
    F -- No --> G((BUY: bracket order<br/>stop -2%, target +6%))
    style G fill:#0072b2,color:#fff
    style X fill:#eee,color:#666
""",
    )


def _rsi2() -> StrategyExplainer:
    return StrategyExplainer(
        name="rsi2",
        title="Connors RSI(2) Mean Reversion",
        tagline="Buy quality stocks when they get briefly oversold inside a long-term uptrend; ride the bounce.",
        status="paper-ready",
        color="#d55e00",
        test_sharpe=1.42,
        test_cagr=0.117,
        test_max_dd=-0.067,
        what_it_does=(
            "Larry Connors's classic mean-reversion play. We require the stock to be in a long-term uptrend "
            "(close above its 200-day SMA), then wait for a very short-term oversold spike — a 2-day RSI below 5. "
            "We buy on that panic and exit a few days later when the RSI rebounds above 70. The thesis: panics "
            "in uptrends are usually overdone and reverse quickly."
        ),
        why_it_works=(
            "When healthy uptrending stocks dip hard for 1-2 days, it's almost always news-noise or general "
            "market wobbles, not a genuine trend reversal. Other traders pile in to 'buy the dip', creating a "
            "predictable bounce. RSI(2) catches the moment of peak panic, then we exit on the relief rally."
        ),
        when_it_works=[
            "Choppy/range-bound markets (2015, 2019) — tons of small reversions",
            "Even in steady uptrends — pullbacks within trends are short-lived bounces",
            "High-vol periods (VIX 18-25) — bigger oversold = bigger bounce",
        ],
        when_it_fails=[
            "Sustained downtrends — a stock at 'RSI(2) < 5' can stay there for weeks while it drops 30%",
            "Cleared market crashes — even the 200-SMA filter lags real regime changes",
            "Single-stock disasters — earnings misses, fraud, M&A breakups; the bracket stop limits damage",
        ],
        glossary=[
            ("RSI(2)",
             "The Relative Strength Index calculated over only 2 days, instead of the standard 14. It "
             "moves much faster — values near 0 mean 'sold off violently in the last 2 days', near 100 "
             "means 'rallied violently'. Larry Connors developed RSI(2) specifically for short-term mean reversion."),
            ("SMA-200 filter",
             "We only buy if the stock's close is above its 200-day simple moving average. This keeps us "
             "out of stocks in long-term downtrends where bounces don't last."),
            ("Mean reversion",
             "The statistical tendency of an extreme value to return toward the long-term average. "
             "Opposite of 'trend continuation' — most strategies exploit one or the other, never both at once."),
            ("Time stop",
             "A safety exit: if neither RSI(70) nor the catastrophe stop has fired in 5 days, we close anyway. "
             "Keeps capital from being trapped in a 'failed reversion' that's quietly turning into a downtrend."),
        ],
        entry_flowchart="""flowchart TD
    A[Daily bar closes] --> B{close > 200-day SMA?}
    B -- No: in long-term downtrend --> X[Skip: wait for tomorrow]
    B -- Yes --> C{RSI-2 < 5?<br/>extreme oversold}
    C -- No: not panicked enough --> X
    C -- Yes --> F{Earnings within 3 days?}
    F -- Yes --> X
    F -- No --> G((BUY: bracket order<br/>stop -3%, target wide<br/>real exit on RSI-2 > 70))
    style G fill:#d55e00,color:#fff
    style X fill:#eee,color:#666
""",
    )


def _donchian() -> StrategyExplainer:
    return StrategyExplainer(
        name="donchian",
        title="Donchian-20 Breakout",
        tagline="Buy stocks breaking to a new 20-day high inside an uptrend; ride with a trailing stop.",
        status="disabled",
        color="#888",
        test_sharpe=-0.21,
        test_cagr=-0.014,
        test_max_dd=-0.109,
        what_it_does=(
            "The original 'Turtles' strategy. When a stock closes above its highest closing price in the "
            "past 20 days, we buy — the breakout signals new buying interest. We don't try to catch a top: "
            "instead we trail a stop 2× ATR below the highest price since entry, ratcheting up as the trade "
            "runs in our favor. The aim is to ride the rare big multi-month winner."
        ),
        why_it_works=(
            "Most retail traders 'buy low, sell high' and dislike paying a new high. Counter-intuitively, "
            "stocks at new highs tend to keep going — momentum begets momentum, and short-sellers covering "
            "their positions adds fuel. The strategy concedes a low win-rate (~30%) in exchange for the "
            "rare 200-300% winners that pay for everything else."
        ),
        when_it_works=[
            "Strong, persistent bull markets (2017, post-COVID 2020 H2, 2024)",
            "Wide universes (Russell 3000, all-US stocks > $5) — more candidates means more big winners",
            "Long holding horizons — the edge takes 3-6+ months to play out",
        ],
        when_it_fails=[
            "Choppy/ranging markets — every 'breakout' reverses; death by a thousand cuts",
            "S&P 500-only universe — mature large-caps don't 5x; we miss the names that do",
            "2-year test windows — too short for a strategy whose edge needs 5-10 years to be visible",
        ],
        glossary=[
            ("Donchian Channel",
             "A simple band defined by the rolling N-day high and low. We use the 20-day version: the upper "
             "channel is the highest close of the past 20 days. Breaking above it = a new local high."),
            ("ATR (Average True Range)",
             "A measure of typical daily price movement. ATR(14) is roughly 'how many dollars does this stock "
             "move on an average day?'. We use it to set stops that scale with volatility — higher-vol stocks "
             "need wider stops or they'd whipsaw."),
            ("Trailing stop",
             "A stop-loss that ratchets UP as the trade moves in your favor, but never moves down. Lets "
             "winners run while protecting accumulated gains. Doesn't help with sudden gap-down events."),
            ("Walk-forward validation",
             "Splitting historical data into a 'train' window and a 'test' window. We tune parameters only "
             "on train data, then run the chosen parameters on the test data we never looked at. If the "
             "strategy collapses on test, the apparent edge was overfit noise. Donchian failed this gate."),
        ],
        entry_flowchart="""flowchart TD
    A[Daily bar closes] --> B{close > 200-day SMA?}
    B -- No: in long-term downtrend --> X[Skip: wait for tomorrow]
    B -- Yes --> V{ATR/close > 1%?<br/>enough volatility}
    V -- No: too sleepy --> X
    V -- Yes --> C{close > 20-day<br/>highest close?}
    C -- No: not a breakout --> X
    C -- Yes --> F{Earnings within 3 days?}
    F -- Yes --> X
    F -- No --> G((BUY: bracket order<br/>hard stop -8%<br/>trailing stop ratchets up))
    style G fill:#888,color:#fff
    style X fill:#eee,color:#666
""",
    )


_REGISTRY: dict[str, StrategyExplainer] = {
    "pullback_ema": _pullback_ema(),
    "rsi2": _rsi2(),
    "donchian": _donchian(),
}


# ----------------- public API -----------------
def list_explainers() -> list[StrategyExplainer]:
    """Return all explainers, in a deterministic display order:
    paper-ready first, then disabled."""
    items = list(_REGISTRY.values())
    items.sort(key=lambda e: (0 if e.status == "paper-ready" else 1, e.name))
    return items


def get_explainer(name: str) -> StrategyExplainer | None:
    """Return one explainer with live params + example trade hydrated."""
    base = _REGISTRY.get(name)
    if base is None:
        return None
    base.params = _live_params(name)
    base.example = _pick_example_trade(name)
    return base


# ----------------- live data -----------------
def _live_params(name: str) -> list[ParamRow]:
    """Read the strategy's YAML and surface the parameters that affect trading."""
    path = CONFIG_DIR / f"{name}.yaml"
    if not path.exists():
        return []
    cfg = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    # Per-strategy whitelists keep the page focused on the parameters that matter
    # — not every YAML key (some are just metadata).
    interesting: dict[str, list[tuple[str, str, str]]] = {
        "pullback_ema": [
            ("ema_fast", "EMA fast period", "the moving average price has to pull back to"),
            ("sma_mid", "SMA mid period", "uptrend filter (close > SMA-mid)"),
            ("sma_slow", "SMA slow period", "long-term uptrend filter (SMA-mid > SMA-slow)"),
            ("rsi_zone", "RSI entry zone", "RSI must fall in this band on entry"),
            ("pullback_proximity_pct", "Pullback proximity", "today's low must come within this % of EMA-fast"),
            ("stop_loss_pct", "Stop loss %", "catastrophe exit"),
            ("take_profit_pct", "Take profit %", "primary winner exit"),
            ("time_stop_days", "Time stop (days)", "force exit if neither hit"),
            ("avoid_earnings_within_days", "Earnings filter window", "skip entries within N days of next earnings"),
            ("capital_allocation_usd", "Capital cap (USD)", "hard limit per strategy"),
        ],
        "rsi2": [
            ("rsi_short_period", "RSI short period", "the RSI window — Connors uses 2 days"),
            ("oversold_threshold", "Oversold threshold", "RSI must be below this to enter"),
            ("exit_threshold", "Exit threshold", "RSI rising above this triggers exit"),
            ("sma_long", "SMA long period", "must be above this to enter (uptrend filter)"),
            ("stop_loss_pct", "Stop loss %", "catastrophe exit"),
            ("time_stop_days", "Time stop (days)", "force exit if RSI doesn't rebound"),
            ("avoid_earnings_within_days", "Earnings filter window", "skip entries within N days of next earnings"),
            ("capital_allocation_usd", "Capital cap (USD)", "hard limit per strategy"),
        ],
        "donchian": [
            ("donchian_period", "Donchian period", "rolling N-day high to break out of"),
            ("sma_long", "SMA long period", "uptrend filter"),
            ("atr_mult", "ATR multiplier", "trailing stop = highest_close - mult × ATR(14)"),
            ("hard_stop_pct", "Hard stop %", "catastrophe floor; never moves"),
            ("min_atr_pct", "Min ATR %", "skip stagnant names"),
            ("time_stop_days", "Time stop (days)", "trend-followers ride extended moves"),
            ("avoid_earnings_within_days", "Earnings filter window", "skip entries within N days of next earnings"),
            ("capital_allocation_usd", "Capital cap (USD)", "hard limit per strategy"),
        ],
    }

    out: list[ParamRow] = []
    for key, label, note in interesting.get(name, []):
        if key in cfg:
            v = cfg[key]
            if isinstance(v, list):
                value = ", ".join(str(x) for x in v)
            elif isinstance(v, float):
                value = f"{v:g}"
            else:
                value = str(v)
            out.append(ParamRow(label=label, value=value, note=note))
    return out


def _pick_example_trade(name: str) -> ExampleTrade | None:
    """Find one representative recent trade in the backtest CSV.

    We look for a take-profit (or signal_exit for RSI(2)) that closed within
    the recent backtest window, with a meaningful return — not a tiny noise
    trade. Ordering is deterministic so the page is stable on refresh.
    """
    sub = BACKTEST_DIR / name
    if not sub.exists():
        return None
    candidates = sorted(sub.glob("*_trades.csv"))
    if not candidates:
        return None

    # Prefer the most recent / largest file
    best_csv = candidates[-1]
    rows: list[dict[str, Any]] = []
    with best_csv.open() as f:
        reader = csv.DictReader(f)
        for r in reader:
            rows.append(r)
    if not rows:
        return None

    # Find a clean winner: take-profit or signal_exit with positive PnL,
    # ordered by entry date so the same one shows up across reloads.
    desired_reasons = {"take_profit", "signal_exit"}
    winners = [r for r in rows
               if r.get("exit_reason") in desired_reasons
               and float(r.get("pnl", 0) or 0) > 0]
    if not winners:
        # Fall back to any closed trade with positive PnL
        winners = [r for r in rows if float(r.get("pnl", 0) or 0) > 0]
    if not winners:
        return None

    # Pick a moderately-sized winner (median pnl among winners) — avoids picking
    # the absolute biggest which might be a fluke single-stock outlier.
    winners.sort(key=lambda r: float(r["pnl"]))
    pick = winners[len(winners) // 2]

    try:
        entry = float(pick["entry_price"])
        exit_p = float(pick["exit_price"])
        return ExampleTrade(
            symbol=pick["symbol"],
            entry_date=pick["entry_date"][:10],
            exit_date=pick["exit_date"][:10],
            entry_price=entry,
            exit_price=exit_p,
            qty=int(float(pick["qty"])),
            pnl=float(pick["pnl"]),
            exit_reason=pick.get("exit_reason", "?"),
            return_pct=(exit_p - entry) / entry if entry else 0.0,
        )
    except (KeyError, ValueError, TypeError):
        return None
