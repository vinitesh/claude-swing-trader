"""Donchian-N breakout — pure trend-following.

Theory: stocks that break to a new N-day (typically 20) high tend to keep going
in the breakout direction. Counter-intuitive at first — most retail traders
"buy the dip" — but it's the original Turtles strategy and forms the
foundation of trend-following CTAs (Dunn, Mulvaney, et al). The trick is
holding through small whipsaws until the rare big runner pays for them.

Entry conditions:
    1. Long-term uptrend filter: close > sma_long (default 200) — keeps us
       out of breakouts in bear regimes.
    2. New high: today's close > rolling N-day max of CLOSES (excluding today).
       Using close-of-close-high avoids the noise of intraday spike highs.
    3. Volatility floor (optional): ATR/close > min_atr_pct so we don't take
       breakouts in flat names where the trend can't generate enough range
       to overcome slippage.

Exits:
    1. Trailing stop: highest_high_since_entry - atr_mult * ATR(14).
       This is the primary exit for winners. The stop ratchets up as the
       trade goes in our favor; never moves down (enforced by backtester).
    2. Hard stop: -hard_stop_pct from entry, never moves. Catastrophe
       protection for new entries before ATR-trail catches up.
    3. Time stop: rarely fires in trend strategies; default 60 days because
       Donchian winners can take months to play out.

Profile vs your other strategies:
    - PullbackEMA (pullback continuation): wins ~40%, holds ~7d, R:R 3:1
    - RSI(2)      (mean reversion):        wins ~70%, holds ~3d, R:R ~1:1
    - Donchian    (breakout trend):        wins ~30%, holds ~30d, R:R 5:1+

Designed to catch the few names that 5x in a year — diversifies away from
the others which structurally cap their upside.
"""

from __future__ import annotations

import pandas as pd
import pandas_ta_classic as ta

from core.signal import Action, Position, Signal
from core.strategy_base import Strategy


class Donchian(Strategy):
    name = "donchian"

    def universe(self) -> list[str]:
        cfg = self.config.get("universe") or []
        return list(cfg)

    def indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        n = int(self.config["donchian_period"])
        # max of CLOSES from the prior N bars (excluding today). Using shift(1)
        # so today's close is compared against history that ENDED yesterday.
        out["donchian_high"] = out["close"].shift(1).rolling(n, min_periods=n).max()
        out["sma_long"] = ta.sma(out["close"], length=int(self.config["sma_long"]))
        out["atr"] = ta.atr(out["high"], out["low"], out["close"], length=14)
        return out

    def should_enter(self, df: pd.DataFrame) -> Signal | None:
        if len(df) < int(self.config["sma_long"]) + int(self.config["donchian_period"]) + 2:
            return None

        last = df.iloc[-1]
        if any(pd.isna(last[c]) for c in ("donchian_high", "sma_long", "atr")):
            return None

        # 1. Uptrend filter on the symbol itself
        if not (last["close"] > last["sma_long"]):
            return None

        # 2. Volatility floor — skip stagnant names
        min_atr_pct = float(self.config.get("min_atr_pct", 0.0))
        if min_atr_pct > 0:
            atr_pct = float(last["atr"]) / float(last["close"]) if last["close"] > 0 else 0
            if atr_pct < min_atr_pct:
                return None

        # 3. Breakout: today's close > the prior N-day max-of-closes
        if not (last["close"] > last["donchian_high"]):
            return None

        # Optional: regime filter (broader market trend)
        if self.config.get("require_bull_regime", False):
            regime = getattr(self, "regime_filter", None)
            if regime is not None and not regime.is_bull(df.index[-1]):
                return None

        # Earnings filter (same hook as other strategies)
        avoid_days = int(self.config.get("avoid_earnings_within_days", 0))
        if avoid_days > 0:
            earnings = getattr(self, "earnings_calendar", None)
            symbol = df.attrs.get("symbol", "")
            if earnings is not None and symbol:
                today = df.index[-1].date() if hasattr(df.index[-1], "date") else None
                if earnings.has_earnings_within(symbol, avoid_days, today=today):
                    return None

        entry = float(last["close"])
        # Hard stop: catastrophic floor that never moves.
        hard_stop = entry * (1 - float(self.config["hard_stop_pct"]))
        # Trailing stop will ratchet up from this initial level — the
        # backtester sets stop_loss = max(current, candidate) each bar.
        # On entry, initial trail = entry - atr_mult * ATR (often inside hard_stop).
        atr_mult = float(self.config["atr_mult"])
        trail_initial = entry - atr_mult * float(last["atr"])
        # Use the LARGER (tighter) of hard_stop and trail_initial as the entry stop.
        # If trail_initial is below hard_stop (very volatile name), prefer hard.
        initial_stop = max(hard_stop, trail_initial)

        # Take-profit set very wide; primary exit is the trailing stop.
        target = entry * (1 + float(self.config.get("take_profit_pct", 1.0)))

        symbol = df.attrs.get("symbol", "UNKNOWN")
        return Signal(
            symbol=symbol,
            action=Action.BUY,
            entry_price=entry,
            stop_loss=initial_stop,
            take_profit=target,
            strategy_name=self.name,
            confidence=0.55,
            metadata={
                "atr": float(last["atr"]),
                "atr_pct": float(last["atr"]) / float(last["close"]),
                "donchian_high": float(last["donchian_high"]),
            },
        )

    def update_trailing_stop(
        self, position: Position, df: pd.DataFrame
    ) -> float | None:
        """ATR-trail: candidate stop = highest close since entry - atr_mult * ATR.

        Computing "highest close since entry" requires knowing when the
        position opened. We use position.opened_at as the cutoff.
        """
        if df.empty:
            return None
        last = df.iloc[-1]
        if pd.isna(last.get("atr")):
            return None

        # Slice df to bars since entry (inclusive of entry day)
        opened = pd.Timestamp(position.opened_at).normalize()
        since = df.loc[df.index >= opened]
        if since.empty:
            return None

        highest = float(since["close"].max())
        atr_mult = float(self.config["atr_mult"])
        return highest - atr_mult * float(last["atr"])

    def time_stop_days(self) -> int:
        """Donchian holds long — most strategies use 5-15 days, we use 60
        because trend-followers explicitly want to ride extended moves."""
        return int(self.config.get("time_stop_days", 60))
