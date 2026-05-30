"""Connors RSI(2) mean-reversion strategy.

Theory: in a long-term uptrend (above 200-day SMA), short-term oversold
conditions (RSI(2) < ~10) tend to mean-revert within a few days. Larry
Connors's books document this on US equities back to ~1990; it's one of
the more robust simple strategies with a long out-of-sample track record.

Entry conditions (ALL must be true on the latest bar):
    1. Long-term uptrend:  close > sma_long (200)
    2. Short-term oversold: rsi_short (RSI(2)) < oversold_threshold

Exit conditions (any of):
    - Indicator: RSI(2) > exit_threshold (~70). Closes at today's close.
    - Bracket stop_loss (catastrophe protection only)
    - Time stop (force exit if neither has fired in N days)

Notes vs PullbackEMA:
    - No "bullish candle" or "pullback proximity" gate — RSI(2) is the gate.
    - No take-profit bracket because exits are indicator-based; the take_profit
      level passed to the broker is set far above to make the bracket order
      effectively "stop only".
"""

from __future__ import annotations

import pandas as pd
import pandas_ta_classic as ta

from core.signal import Action, Position, Signal
from core.strategy_base import Strategy


class RSI2(Strategy):
    name = "rsi2"

    def universe(self) -> list[str]:
        cfg = self.config.get("universe") or []
        return list(cfg)

    def indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        out["rsi_short"] = ta.rsi(out["close"], length=int(self.config["rsi_short_period"]))
        out["sma_long"] = ta.sma(out["close"], length=int(self.config["sma_long"]))
        return out

    def should_enter(self, df: pd.DataFrame) -> Signal | None:
        if len(df) < int(self.config["sma_long"]) + 1:
            return None

        last = df.iloc[-1]
        if any(pd.isna(last[c]) for c in ("rsi_short", "sma_long")):
            return None

        # Optional regime filter (broader market trend)
        if self.config.get("require_bull_regime", False):
            regime = getattr(self, "regime_filter", None)
            if regime is not None and not regime.is_bull(df.index[-1]):
                return None

        # 1. Long-term uptrend on the symbol itself
        if not (last["close"] > last["sma_long"]):
            return None

        # 2. Short-term oversold
        oversold = float(self.config["oversold_threshold"])
        if not (last["rsi_short"] < oversold):
            return None

        entry = float(last["close"])
        # Stop loss is catastrophe protection only; primary exit is indicator.
        stop = entry * (1 - float(self.config["stop_loss_pct"]))
        # Take-profit set very wide so the bracket effectively only stops.
        # We exit on RSI > exit_threshold via should_exit_signal().
        target = entry * (1 + float(self.config.get("take_profit_pct", 0.20)))

        symbol = df.attrs.get("symbol", "UNKNOWN")
        return Signal(
            symbol=symbol,
            action=Action.BUY,
            entry_price=entry,
            stop_loss=stop,
            take_profit=target,
            strategy_name=self.name,
            confidence=0.55,
            metadata={
                "rsi_short": float(last["rsi_short"]),
                "sma_long": float(last["sma_long"]),
            },
        )

    def should_exit_signal(self, position: Position, df: pd.DataFrame) -> bool:
        """Exit when short-term momentum has reverted (RSI(2) > exit threshold)."""
        if df.empty:
            return False
        last = df.iloc[-1]
        if pd.isna(last.get("rsi_short")):
            return False
        return float(last["rsi_short"]) > float(self.config["exit_threshold"])

    def time_stop_days(self) -> int:
        return int(self.config.get("time_stop_days", 5))
