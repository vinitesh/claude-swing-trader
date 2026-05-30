"""Pullback to 20 EMA strategy.

Buys quality stocks in confirmed uptrends when they pull back to the 20 EMA
and show a bullish bounce candle. Exits via 2:1 take-profit / stop-loss
bracket orders, plus an optional time stop handled by the engine.

Entry conditions (ALL must be true on the latest bar):
    1. Uptrend:           close > sma_mid (50) > sma_slow (200)
    2. Pullback:          today's low within `pullback_proximity_pct` of ema_fast (20)
    3. Bullish candle:    today's close > today's open
    4. RSI in zone:       rsi_zone[0] <= rsi(14) <= rsi_zone[1]

Exit (handled by bracket orders):
    Stop loss   = entry * (1 - stop_loss_pct)
    Take profit = entry * (1 + take_profit_pct)
"""

from __future__ import annotations

import pandas as pd
import pandas_ta_classic as ta

from core.signal import Action, Signal
from core.strategy_base import Strategy


class PullbackEMA(Strategy):
    name = "pullback_ema"

    def universe(self) -> list[str]:
        cfg = self.config.get("universe") or []
        return list(cfg)

    def indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        out["ema_fast"] = ta.ema(out["close"], length=int(self.config["ema_fast"]))
        out["sma_mid"] = ta.sma(out["close"], length=int(self.config["sma_mid"]))
        out["sma_slow"] = ta.sma(out["close"], length=int(self.config["sma_slow"]))
        out["rsi"] = ta.rsi(out["close"], length=int(self.config["rsi_period"]))
        return out

    def should_enter(self, df: pd.DataFrame) -> Signal | None:
        if len(df) < int(self.config["sma_slow"]) + 1:
            return None  # not enough history

        last = df.iloc[-1]

        # Regime filter (optional): suppress longs when broader market is below
        # its trend filter. The backtester / engine attaches `regime_filter` to
        # the strategy at runtime; absence means filter is off.
        if self.config.get("require_bull_regime", True):
            regime = getattr(self, "regime_filter", None)
            if regime is not None and not regime.is_bull(df.index[-1]):
                return None

        # Drop bars without all indicators populated
        if any(pd.isna(last[c]) for c in ("ema_fast", "sma_mid", "sma_slow", "rsi")):
            return None

        # 1. Uptrend filter
        if self.config.get("require_uptrend", True):
            if not (last["close"] > last["sma_mid"] > last["sma_slow"]):
                return None

        # 2. Pullback proximity
        proximity = float(self.config["pullback_proximity_pct"])
        if last["ema_fast"] <= 0:
            return None
        dist_to_ema = abs(last["low"] - last["ema_fast"]) / last["ema_fast"]
        if dist_to_ema > proximity:
            return None

        # 3. Bullish candle
        if self.config.get("require_bullish_candle", True):
            if last["close"] <= last["open"]:
                return None

        # 4. RSI zone
        lo, hi = self.config["rsi_zone"]
        if not (float(lo) <= float(last["rsi"]) <= float(hi)):
            return None

        # All filters passed — build the signal
        entry = float(last["close"])
        stop = entry * (1 - float(self.config["stop_loss_pct"]))
        target = entry * (1 + float(self.config["take_profit_pct"]))

        symbol = df.attrs.get("symbol", "UNKNOWN")
        return Signal(
            symbol=symbol,
            action=Action.BUY,
            entry_price=entry,
            stop_loss=stop,
            take_profit=target,
            strategy_name=self.name,
            confidence=0.65,
            metadata={
                "rsi": float(last["rsi"]),
                "ema_fast": float(last["ema_fast"]),
                "dist_to_ema_pct": float(dist_to_ema),
            },
        )

    def time_stop_days(self) -> int:
        """How many bars until we force-exit a flat position."""
        return int(self.config.get("time_stop_days", 15))
