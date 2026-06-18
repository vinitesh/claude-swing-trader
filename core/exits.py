"""Shared exit-decision logic used by BOTH the backtester and the live runner.

Keeping these decisions in one place guarantees that live trading exits a
position the same way the strategy was validated in backtest. The functions are
PURE decisions: they read (strategy, position, price-window) and return what to
do. The caller executes the action against its own broker — simulated daily bars
in the backtester, real Alpaca orders live.

Scope: indicator/time-driven exits only —
  * trailing-stop ratchet  (strategy.update_trailing_stop)
  * signal exit            (strategy.should_exit_signal, e.g. RSI(2) > 70)
  * time stop              (strategy.time_stop_days)
Fixed price-trigger exits (stop-loss / take-profit) are NOT here: live they are
the Alpaca bracket legs the broker fills automatically; in backtest they are
simulated by Backtester._check_bracket_exit. This module deliberately leaves
those to the broker so we never double-handle them.
"""

from __future__ import annotations

from datetime import date, datetime

import pandas as pd

from core.signal import Position
from core.strategy_base import Strategy


def compute_trailing_stop(
    strategy: Strategy, position: Position, window: pd.DataFrame
) -> float | None:
    """Return a new stop price if the strategy's trailing stop ratchets UP.

    Long-only ratchet (mirrors the backtester): adopt the strategy's candidate
    stop only if it is strictly higher than the current stop. Returns None when
    the stop should not move (strategy returned None, not long, or candidate is
    not higher).
    """
    new_stop = strategy.update_trailing_stop(position, window)
    if (
        new_stop is not None
        and position.side == "long"
        and position.stop_loss is not None
        and float(new_stop) > float(position.stop_loss)
    ):
        return float(new_stop)
    return None


def indicator_exit_reason(
    strategy: Strategy,
    position: Position,
    window: pd.DataFrame,
    today: date | datetime,
) -> str | None:
    """Return 'signal_exit' or 'time_stop' if an indicator/time exit fires, else None.

    Signal exit is checked before the time stop (same precedence the backtester
    uses). Does not consider bracket SL/TP — see module docstring.
    """
    if strategy.should_exit_signal(position, window):
        return "signal_exit"

    time_stop = int(getattr(strategy, "time_stop_days", lambda: 999)())
    opened = position.opened_at
    opened_date = opened.date() if isinstance(opened, datetime) else opened
    today_date = today.date() if isinstance(today, datetime) else today
    held = (today_date - opened_date).days
    if held >= time_stop:
        return "time_stop"
    return None
