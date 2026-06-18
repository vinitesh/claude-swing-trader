"""Integration tests for LiveRunner.manage_exits — the live exit pass.

Critical invariants (from the code review of this feature):
  1. A signal/time exit CANCELS the symbol's bracket legs BEFORE the market
     close (else a resting stop/TP could fill after and flip us short).
  2. manage_exits does NOT close the DB row itself — sync is the single close
     authority and records the real fill. The row stays is_open=True here.
  3. Only positions the broker actually HOLDS are acted on (an already-filled
     exit is skipped, not re-submitted).
  4. A ratcheted trailing stop is pushed to the broker and mirrored in the DB.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import patch

from persistence import repository as repo
from persistence.db import session_scope
from strategies.pullback_ema import PullbackEMA
from strategies.rsi2 import RSI2
from tests.test_live_runner import runner_factory, isolated_db  # noqa: F401


def _seed_open_position(symbol, strategy, qty=10, entry=100.0, opened_days_ago=0):
    with session_scope() as s:
        repo.open_position(
            s, symbol=symbol, strategy_name=strategy, side="long",
            qty=qty, avg_entry_price=entry, stop_loss=entry * 0.97,
            take_profit=entry * 1.2,
        )
        # backdate opened_at for time-stop tests
        if opened_days_ago:
            row = repo.get_open_positions(s)[-1]
            row.opened_at = datetime.utcnow() - timedelta(days=opened_days_ago)


def test_signal_exit_cancels_legs_and_defers_db_close(runner_factory):
    runner = runner_factory(dry_run=False, strategies=["rsi2"], universe=["AAPL"])
    _seed_open_position("AAPL", "rsi2")
    # Broker must report it held so manage_exits acts on it.
    runner.broker.submitted = []
    from core.signal import Position
    runner.broker.positions = [Position(
        symbol="AAPL", qty=10, avg_entry_price=100.0, side="long",
        strategy_name="rsi2", opened_at=datetime.utcnow(),
        stop_loss=97.0, take_profit=120.0,
    )]

    with patch.object(RSI2, "should_exit_signal", return_value=True):
        out = runner.manage_exits()

    assert out.signal_exits == 1, out
    # 1: bracket legs canceled BEFORE close
    assert "AAPL" in runner.broker.canceled_legs
    # 1: market close submitted (FakeBroker.close_position drops it)
    assert all(p.symbol != "AAPL" for p in runner.broker.positions)
    # 2: DB row NOT closed here — sync owns that
    with session_scope() as s:
        still_open = [p.symbol for p in repo.get_open_positions(s)]
    assert "AAPL" in still_open, "manage_exits must NOT close the DB row (defer to sync)"


def test_skips_symbol_broker_does_not_hold(runner_factory):
    runner = runner_factory(dry_run=False, strategies=["rsi2"], universe=["AAPL"])
    _seed_open_position("AAPL", "rsi2")
    runner.broker.positions = []  # broker flat — exit already filled

    with patch.object(RSI2, "should_exit_signal", return_value=True):
        out = runner.manage_exits()

    assert out.signal_exits == 0
    assert "AAPL" not in runner.broker.canceled_legs
    assert runner.broker.canceled_legs == []


def test_dry_run_places_no_orders(runner_factory):
    runner = runner_factory(dry_run=True, strategies=["rsi2"], universe=["AAPL"])
    _seed_open_position("AAPL", "rsi2")
    from core.signal import Position
    runner.broker.positions = [Position(
        symbol="AAPL", qty=10, avg_entry_price=100.0, side="long",
        strategy_name="rsi2", opened_at=datetime.utcnow(),
        stop_loss=97.0, take_profit=120.0,
    )]

    with patch.object(RSI2, "should_exit_signal", return_value=True):
        out = runner.manage_exits()

    assert out.signal_exits == 1          # decision recorded
    assert runner.broker.canceled_legs == []   # but no orders placed
    assert any(p.symbol == "AAPL" for p in runner.broker.positions)


def test_partial_cancel_failure_aborts_close(runner_factory):
    # If bracket legs can't ALL be canceled, we must NOT market-close — a
    # surviving leg could fill after our sell and flip us short (the CRITICAL).
    runner = runner_factory(dry_run=False, strategies=["rsi2"], universe=["AAPL"])
    _seed_open_position("AAPL", "rsi2")
    from core.signal import Position
    runner.broker.positions = [Position(
        symbol="AAPL", qty=10, avg_entry_price=100.0, side="long",
        strategy_name="rsi2", opened_at=datetime.utcnow(),
        stop_loss=97.0, take_profit=120.0,
    )]
    runner.broker.cancel_succeeds = False  # simulate a leg cancel failing

    with patch.object(RSI2, "should_exit_signal", return_value=True):
        out = runner.manage_exits()

    assert out.signal_exits == 0
    assert out.errors == 1
    assert runner.broker.closed == [], "must NOT close when cancel did not fully succeed"
    # position still held at broker (no naked sell happened)
    assert any(p.symbol == "AAPL" for p in runner.broker.positions)


def test_trailing_stop_raised_to_broker_and_db(runner_factory):
    runner = runner_factory(dry_run=False, strategies=["donchian"], universe=["AAPL"])
    _seed_open_position("AAPL", "donchian", entry=100.0)
    from core.signal import Position
    runner.broker.positions = [Position(
        symbol="AAPL", qty=10, avg_entry_price=100.0, side="long",
        strategy_name="donchian", opened_at=datetime.utcnow(),
        stop_loss=97.0, take_profit=120.0,
    )]

    from strategies.donchian import Donchian
    with patch.object(Donchian, "update_trailing_stop", return_value=105.0), \
         patch.object(Donchian, "should_exit_signal", return_value=False):
        out = runner.manage_exits()

    assert out.stops_raised == 1
    assert ("AAPL", 105.0) in runner.broker.stop_raises
    with session_scope() as s:
        row = [p for p in repo.get_open_positions(s) if p.symbol == "AAPL"][0]
        assert row.stop_loss == 105.0  # mirrored into DB on success
