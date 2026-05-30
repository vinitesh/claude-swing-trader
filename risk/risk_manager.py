"""RiskManager: gates every signal before it becomes an order."""

from __future__ import annotations

from dataclasses import dataclass

from core.signal import Position, Signal
from execution.broker_base import Account


@dataclass
class RiskLimits:
    max_open_positions: int = 8
    max_per_strategy: int = 5
    daily_loss_limit_pct: float = 0.03
    min_position_size_usd: float = 100.0
    min_reward_to_risk: float = 1.5


class RiskManager:
    """Centralized risk gate. Returns True if the signal may be executed."""

    def __init__(self, limits: RiskLimits | None = None):
        self.limits = limits or RiskLimits()

    def approve(
        self,
        signal: Signal,
        account: Account,
        open_positions: list[Position],
        qty: int,
        day_pnl: float = 0.0,
        day_start_equity: float | None = None,
        strategy_remaining_capital: float | None = None,
    ) -> tuple[bool, str]:
        """Return (approved, reason). reason is empty when approved.

        ``strategy_remaining_capital`` is the per-strategy capital cap minus the
        notional value of already-open positions tagged to that strategy. When
        provided (multi-strategy live mode), this strategy cannot exceed it.
        """

        if qty <= 0:
            return False, "qty=0 (insufficient capital or zero risk distance)"

        # Position-size sanity
        notional = qty * signal.entry_price
        if notional < self.limits.min_position_size_usd:
            return False, f"notional ${notional:.0f} below min ${self.limits.min_position_size_usd:.0f}"

        # Real available cash = cash on hand (we don't use margin in backtest)
        if notional > account.cash:
            return False, f"notional ${notional:.0f} exceeds cash ${account.cash:.0f}"
        if notional > account.buying_power:
            return False, f"notional ${notional:.0f} exceeds buying power ${account.buying_power:.0f}"

        # Per-strategy capital cap (multi-strategy isolation)
        if strategy_remaining_capital is not None:
            if notional > strategy_remaining_capital:
                return False, (
                    f"notional ${notional:.0f} exceeds strategy capital remaining "
                    f"${strategy_remaining_capital:.0f}"
                )

        # Reward:risk
        if signal.risk_per_share <= 0:
            return False, "risk_per_share<=0 (entry==stop)"
        if signal.reward_to_risk < self.limits.min_reward_to_risk:
            return False, (
                f"reward:risk {signal.reward_to_risk:.2f} below min "
                f"{self.limits.min_reward_to_risk:.2f}"
            )

        # Open-position counts
        if len(open_positions) >= self.limits.max_open_positions:
            return False, f"max open positions ({self.limits.max_open_positions}) reached"

        per_strat = sum(1 for p in open_positions if p.strategy_name == signal.strategy_name)
        if per_strat >= self.limits.max_per_strategy:
            return False, (
                f"max per-strategy positions for {signal.strategy_name} "
                f"({self.limits.max_per_strategy}) reached"
            )

        # Already in this symbol?
        if any(p.symbol == signal.symbol for p in open_positions):
            return False, f"already have a position in {signal.symbol}"

        # Daily loss circuit breaker
        if day_start_equity and day_start_equity > 0:
            loss_pct = -day_pnl / day_start_equity if day_pnl < 0 else 0.0
            if loss_pct >= self.limits.daily_loss_limit_pct:
                return False, (
                    f"daily loss limit hit: -{loss_pct*100:.2f}% "
                    f">= {self.limits.daily_loss_limit_pct*100:.2f}%"
                )

        return True, ""
