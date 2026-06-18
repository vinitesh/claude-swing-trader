"""LiveRunner integration tests.

We mock the broker (no Alpaca API), use the real strategy on cached parquet
data, and use an in-memory SQLite. This proves the full path: scan → signal →
risk → submit → persist → idempotency.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import pytest

from core.config import load_strategy_config
from core.live_runner import LiveRunner
from core.signal import Action, Position, Signal as DomainSignal
from data.provider_base import DataProvider
from execution.broker_base import Account, Broker
from notifications.notifier_base import NullNotifier


CACHE_DIR = Path(__file__).resolve().parent.parent / "data_cache"


# ---------------- fakes ----------------
class FakeBroker(Broker):
    name = "fake"

    def __init__(self, cash: float = 100_000.0, fail_submit: bool = False):
        self.cash = cash
        self.equity = cash
        self.positions: list[Position] = []
        self.submitted: list[tuple[DomainSignal, int]] = []
        self.fail_submit = fail_submit
        self._next_id = 0
        self.canceled_legs: list = []
        self.stop_raises: list = []
        self.closed: list = []
        self.cancel_succeeds: bool = True

    def get_account(self) -> Account:
        return Account(cash=self.cash, equity=self.equity, buying_power=self.cash)

    def get_positions(self) -> list[Position]:
        return list(self.positions)

    def submit_bracket_order(self, signal: DomainSignal, qty: int) -> str:
        if self.fail_submit:
            raise RuntimeError("simulated broker outage")
        self._next_id += 1
        oid = f"fake-{self._next_id}"
        self.submitted.append((signal, qty))
        # Simulate fill: deduct cash, add position
        self.cash -= signal.entry_price * qty
        from datetime import datetime
        self.positions.append(Position(
            symbol=signal.symbol, qty=qty, avg_entry_price=signal.entry_price,
            side="long", strategy_name=signal.strategy_name,
            opened_at=datetime.utcnow(),
            stop_loss=signal.stop_loss, take_profit=signal.take_profit,
        ))
        return oid

    def close_position(self, symbol: str) -> None:
        self.closed.append(symbol)
        self.positions = [p for p in self.positions if p.symbol != symbol]

    def is_market_open(self) -> bool:
        return True

    # Exit-management hooks (recorded for assertions)
    def cancel_orders_for_symbol(self, symbol: str) -> bool:
        self.canceled_legs.append(symbol)
        return self.cancel_succeeds

    def update_stop_price(self, symbol: str, new_stop: float) -> bool:
        self.stop_raises.append((symbol, round(float(new_stop), 2)))
        return True


class CachedProvider(DataProvider):
    """Reads parquet bars from data_cache/, returns the slice for any window."""

    name = "cached"

    def get_bars(self, symbol, start, end):
        candidates = sorted(CACHE_DIR.glob(f"{symbol}_*.parquet"))
        if not candidates:
            raise ValueError(f"No cached parquet for {symbol}")
        df = pd.read_parquet(candidates[-1])
        df.columns = [c.lower() for c in df.columns]
        if start is not None:
            df = df.loc[str(start):]
        if end is not None:
            df = df.loc[:str(end)]
        df.attrs["symbol"] = symbol
        return df


# ---------------- fixtures ----------------
@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    """Point persistence at a fresh per-test SQLite file."""
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    # Reset the persistence module's cached engine/sessionmaker
    import persistence.db as db_mod
    db_mod._engine = None
    db_mod._SessionLocal = None
    yield db_path


@pytest.fixture()
def runner_factory(isolated_db, tmp_path):
    """Construct a LiveRunner pointed at fakes."""
    def _build(*, dry_run: bool = False, strategies: list[str] | None = None,
               fail_submit: bool = False, universe: list[str] | None = None) -> LiveRunner:
        # Minimal real-shaped settings
        class S:
            log_level = "WARNING"
            log_dir = str(tmp_path / "logs")
            trading_mode = "paper"
            alpaca_api_key = "x"
            alpaca_secret_key = "x"
            alpaca_data_feed = "iex"
            telegram_bot_token = ""
            telegram_chat_id = ""
            database_url = f"sqlite:///{isolated_db}"
        cfg = {
            "broker": {"type": "alpaca"},
            "data": {"primary": "yfinance", "cache_dir": str(CACHE_DIR)},
            "risk": {
                "max_open_positions": 8, "max_per_strategy": 5,
                "daily_loss_limit_pct": 0.03, "min_position_size_usd": 100,
            },
            "universe": {"default": universe or ["AAPL", "MSFT", "NVDA"]},
            "strategies": [
                {"name": s, "enabled": True, "config_file": f"{s}.yaml"}
                for s in (strategies or ["pullback_ema"])
            ],
            "notifications": {"telegram": {"enabled": False}},
        }
        return LiveRunner(
            settings=S(), config=cfg,
            broker=FakeBroker(fail_submit=fail_submit),
            data_provider=CachedProvider(),
            notifier=NullNotifier(),
            dry_run=dry_run,
        )
    return _build


# ---------------- tests ----------------
def test_dry_run_does_not_submit(runner_factory, isolated_db):
    runner = runner_factory(dry_run=True)
    outcome = runner.run()
    # Even if there are signals today, dry-run records them as dry-run, no broker call
    assert outcome.error is None
    assert len(runner.broker.submitted) == 0


def test_idempotent_rerun_does_not_double_submit(runner_factory):
    """Run twice — second call should skip the same-day signals."""
    # We monkeypatch should_enter to FORCE a signal for AAPL on the first run,
    # so the test is deterministic regardless of today's market data.
    from strategies.pullback_ema import PullbackEMA
    forced = DomainSignal(
        symbol="AAPL", action=Action.BUY,
        entry_price=150.0, stop_loss=147.0, take_profit=159.0,
        strategy_name="pullback_ema", confidence=0.65,
    )
    with patch.object(PullbackEMA, "should_enter", return_value=forced):
        runner1 = runner_factory(dry_run=False, universe=["AAPL"])
        out1 = runner1.run()
        assert out1.orders_submitted == 1
        assert len(runner1.broker.submitted) == 1
        # Re-run: should detect already-acted and skip
        runner2 = runner_factory(dry_run=False, universe=["AAPL"])
        # Inject the same broker so positions persist (real Alpaca would have it too)
        runner2.broker.positions = list(runner1.broker.positions)
        out2 = runner2.run()
        assert out2.orders_submitted == 0
        assert out2.orders_skipped == 1


def test_broker_failure_is_persisted_as_rejected(runner_factory):
    from strategies.pullback_ema import PullbackEMA
    forced = DomainSignal(
        symbol="AAPL", action=Action.BUY,
        entry_price=150.0, stop_loss=147.0, take_profit=159.0,
        strategy_name="pullback_ema", confidence=0.65,
    )
    with patch.object(PullbackEMA, "should_enter", return_value=forced):
        runner = runner_factory(dry_run=False, fail_submit=True, universe=["AAPL"])
        outcome = runner.run()
    assert outcome.orders_submitted == 0
    assert outcome.orders_rejected == 1


def test_no_signal_means_no_orders(runner_factory):
    """If strategy returns None, runner emits nothing."""
    from strategies.pullback_ema import PullbackEMA
    with patch.object(PullbackEMA, "should_enter", return_value=None):
        runner = runner_factory(dry_run=False, universe=["AAPL", "MSFT"])
        outcome = runner.run()
    assert outcome.signals_found == 0
    assert outcome.orders_submitted == 0
    assert len(runner.broker.submitted) == 0


def test_unknown_strategy_in_yaml_skipped_not_crashed(isolated_db, tmp_path):
    """Regression for Jun 1, 2026 outage: when YAML enables a strategy whose
    Python module isn't deployed (image build skew), runner used to raise
    KeyError and abort the entire run, taking the other working strategies
    with it. Now it should log+alert, skip the missing strategy, and let
    valid ones still run.
    """
    from core.live_runner import LiveRunner
    from notifications.notifier_base import NullNotifier
    class S:
        log_level = "WARNING"
        log_dir = str(tmp_path / "logs")
        trading_mode = "paper"
        alpaca_api_key = "x"; alpaca_secret_key = "x"; alpaca_data_feed = "iex"
        telegram_bot_token = ""; telegram_chat_id = ""
        database_url = f"sqlite:///{isolated_db}"
    cfg = {
        "broker": {"type": "alpaca"},
        "data": {"primary": "yfinance", "cache_dir": str(CACHE_DIR)},
        "risk": {"max_open_positions": 8, "max_per_strategy": 5,
                 "daily_loss_limit_pct": 0.03, "min_position_size_usd": 100},
        "universe": {"default": ["AAPL"]},
        "strategies": [
            {"name": "pullback_ema", "enabled": True, "config_file": "pullback_ema.yaml"},
            {"name": "fictional_strategy", "enabled": True, "config_file": "fake.yaml"},
        ],
        "notifications": {"telegram": {"enabled": False}},
    }
    runner = LiveRunner(
        settings=S(), config=cfg,
        broker=FakeBroker(), data_provider=CachedProvider(),
        notifier=NullNotifier(), dry_run=True,
    )
    # The unknown strategy should be skipped, not crash construction.
    assert {s.name for s in runner.strategies} == {"pullback_ema"}
