"""Per-strategy capital allocation tests.

Two layers:
  1. RiskManager.approve() respects `strategy_remaining_capital` parameter.
  2. LiveRunner stops opening positions for a strategy that has consumed its cap.
"""

from __future__ import annotations

from datetime import date
from unittest.mock import patch

import pytest

from core.signal import Action, Position, Signal as DomainSignal
from execution.broker_base import Account
from risk.risk_manager import RiskLimits, RiskManager
from tests.test_live_runner import CachedProvider, FakeBroker, runner_factory, isolated_db  # noqa: F401


# ---------- Risk-manager unit tests ----------
def _sig(symbol="AAPL", entry=100.0):
    return DomainSignal(
        symbol=symbol, action=Action.BUY,
        entry_price=entry, stop_loss=entry*0.98, take_profit=entry*1.06,
        strategy_name="pullback_ema",
    )


def test_risk_approves_within_strategy_cap():
    rm = RiskManager(RiskLimits(min_position_size_usd=100))
    ok, reason = rm.approve(
        signal=_sig(entry=100.0),
        account=Account(cash=50_000, equity=50_000, buying_power=50_000),
        open_positions=[],
        qty=200,                       # notional 20_000
        strategy_remaining_capital=25_000,
    )
    assert ok, reason


def test_risk_rejects_above_strategy_cap():
    rm = RiskManager(RiskLimits(min_position_size_usd=100))
    ok, reason = rm.approve(
        signal=_sig(entry=100.0),
        account=Account(cash=50_000, equity=50_000, buying_power=50_000),
        open_positions=[],
        qty=300,                       # notional 30_000
        strategy_remaining_capital=25_000,
    )
    assert not ok
    assert "strategy capital remaining" in reason


def test_risk_no_cap_when_param_omitted():
    rm = RiskManager(RiskLimits(min_position_size_usd=100))
    ok, _ = rm.approve(
        signal=_sig(entry=100.0),
        account=Account(cash=50_000, equity=50_000, buying_power=50_000),
        open_positions=[],
        qty=300,
        strategy_remaining_capital=None,
    )
    assert ok


# ---------- LiveRunner integration tests ----------
def test_live_runner_respects_strategy_cap(runner_factory):
    """Force two signals on different symbols at $1000 each — strategy cap of
    $1500 should let exactly ONE through."""
    from strategies.pullback_ema import PullbackEMA

    def force_signal(_self, df):
        sym = df.attrs.get("symbol", "X")
        return DomainSignal(
            symbol=sym, action=Action.BUY,
            entry_price=100.0, stop_loss=98.0, take_profit=106.0,
            strategy_name="pullback_ema", confidence=0.65,
        )

    with patch.object(PullbackEMA, "should_enter", force_signal):
        runner = runner_factory(dry_run=False, universe=["AAPL", "MSFT", "NVDA"])
        # Override strategy cap to a low number so the test is deterministic
        runner.strategies[0].capital_allocation_usd = 1500.0
        # Make risk manager generous so cap is the only binding constraint
        runner.risk.limits.min_position_size_usd = 50
        runner.strategies[0].risk_pct = 0.10        # so qty isn't 0 from risk math
        runner.strategies[0].allocation_pct = 1.0   # don't let alloc_pct shrink qty
        outcome = runner.run()

    # Exactly one order goes through; the next ones blow the cap and get rejected
    assert outcome.orders_submitted == 1
    assert outcome.orders_rejected >= 1


def test_live_runner_no_cap_means_no_per_strategy_limit(runner_factory):
    """When capital_allocation_usd=0, runner should NOT cap by strategy capital.

    We size each position small (allocation_pct=0.10 → ~$10k notional) so two
    fills both fit in the $100k starting cash; this isolates the test from
    cash-exhaustion effects and proves the cap doesn't kick in.
    """
    from strategies.pullback_ema import PullbackEMA

    def force_signal(_self, df):
        sym = df.attrs.get("symbol", "X")
        return DomainSignal(
            symbol=sym, action=Action.BUY,
            entry_price=100.0, stop_loss=98.0, take_profit=106.0,
            strategy_name="pullback_ema", confidence=0.65,
        )

    with patch.object(PullbackEMA, "should_enter", force_signal):
        runner = runner_factory(dry_run=False, universe=["AAPL", "MSFT"])
        runner.strategies[0].capital_allocation_usd = 0  # disabled
        runner.risk.limits.min_position_size_usd = 50
        runner.risk.limits.max_per_strategy = 10
        runner.strategies[0].risk_pct = 0.01
        runner.strategies[0].allocation_pct = 0.10  # ~$10k each, fits 2× in $100k
        outcome = runner.run()

    # Both signals should make it through (no cap applied)
    assert outcome.orders_submitted == 2, f"got: {outcome}"
