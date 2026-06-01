"""Reporting layer tests — verify per-strategy slicing math is correct."""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.reporting import report_all_strategies, report_strategy
from core.signal import Action, Signal as DomainSignal
from persistence import repository as repo
from persistence.models import Base


@pytest.fixture()
def session():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    s = Session()
    try:
        yield s
        s.commit()
    finally:
        s.close()


def _seed_two_strategies(session):
    """Seed:
      - pullback_ema: 2 signals, 2 orders, 1 closed win (+$100), 1 closed loss (-$50)
      - rsi2:        3 signals, 3 orders, 2 closed wins (+$200, +$50), 1 still open
    """
    run = repo.start_run(session, mode="paper", strategies=["pullback_ema", "rsi2"])

    # ---- pullback_ema ----
    sig1 = repo.record_signal(session, run,
        DomainSignal(symbol="AAPL", action=Action.BUY,
                     entry_price=100, stop_loss=98, take_profit=106,
                     strategy_name="pullback_ema"),
        bar_date=date(2026, 1, 5))[0]
    repo.record_order_submitted(session, sig1, broker="alpaca", broker_order_id="a1",
        symbol="AAPL", side="buy", qty=10,
        entry_price=100, stop_loss=98, take_profit=106)
    p = repo.open_position(session, symbol="AAPL", strategy_name="pullback_ema",
        side="long", qty=10, avg_entry_price=100, stop_loss=98, take_profit=106)
    p.opened_at = datetime(2026, 1, 5)
    repo.close_position(session, symbol="AAPL", exit_price=110, exit_reason="take_profit",
        closed_at=datetime(2026, 1, 8))   # +$100, 3-day hold

    sig2 = repo.record_signal(session, run,
        DomainSignal(symbol="MSFT", action=Action.BUY,
                     entry_price=200, stop_loss=196, take_profit=212,
                     strategy_name="pullback_ema"),
        bar_date=date(2026, 1, 10))[0]
    repo.record_order_submitted(session, sig2, broker="alpaca", broker_order_id="a2",
        symbol="MSFT", side="buy", qty=5,
        entry_price=200, stop_loss=196, take_profit=212)
    p = repo.open_position(session, symbol="MSFT", strategy_name="pullback_ema",
        side="long", qty=5, avg_entry_price=200, stop_loss=196, take_profit=212)
    p.opened_at = datetime(2026, 1, 10)
    repo.close_position(session, symbol="MSFT", exit_price=190, exit_reason="stop_loss",
        closed_at=datetime(2026, 1, 11))  # -$50, 1-day hold

    # ---- rsi2 ----
    for i, (sym, entry, exit_p, exit_reason, days, pnl_each) in enumerate([
        ("NVDA", 400, 410, "signal_exit", 2, 100),  # +$100 (10 shares × $10)
        ("GOOGL", 140, 145, "take_profit", 3, 50),  # +$50 (10 shares × $5)
    ]):
        sig = repo.record_signal(session, run,
            DomainSignal(symbol=sym, action=Action.BUY,
                         entry_price=entry, stop_loss=entry*0.97, take_profit=entry*1.20,
                         strategy_name="rsi2"),
            bar_date=date(2026, 1, 6+i))[0]
        repo.record_order_submitted(session, sig, broker="alpaca",
            broker_order_id=f"r{i}", symbol=sym, side="buy", qty=10,
            entry_price=entry, stop_loss=entry*0.97, take_profit=entry*1.20)
        p = repo.open_position(session, symbol=sym, strategy_name="rsi2",
            side="long", qty=10, avg_entry_price=entry,
            stop_loss=entry*0.97, take_profit=entry*1.20)
        p.opened_at = datetime(2026, 1, 6+i)
        repo.close_position(session, symbol=sym, exit_price=exit_p, exit_reason=exit_reason,
            closed_at=datetime(2026, 1, 6+i+days))

    # rsi2 still-open position
    sig_open = repo.record_signal(session, run,
        DomainSignal(symbol="TSLA", action=Action.BUY,
                     entry_price=250, stop_loss=242, take_profit=300,
                     strategy_name="rsi2"),
        bar_date=date(2026, 1, 15))[0]
    repo.record_order_submitted(session, sig_open, broker="alpaca", broker_order_id="r-open",
        symbol="TSLA", side="buy", qty=4,
        entry_price=250, stop_loss=242, take_profit=300)
    repo.open_position(session, symbol="TSLA", strategy_name="rsi2",
        side="long", qty=4, avg_entry_price=250, stop_loss=242, take_profit=300)


def test_report_strategy_pullback_ema(session):
    _seed_two_strategies(session)
    r = report_strategy(session, "pullback_ema")
    assert r.signals_total == 2
    assert r.orders_submitted == 2
    assert r.positions_open == 0
    assert r.positions_closed == 2
    assert r.num_wins == 1
    assert r.num_losses == 1
    assert r.realized_pnl == pytest.approx(50.0)         # +100 -50
    assert r.win_rate == pytest.approx(0.5)
    assert r.avg_win == pytest.approx(100.0)
    assert r.avg_loss == pytest.approx(-50.0)
    assert r.profit_factor == pytest.approx(2.0)         # 100/50
    assert r.avg_hold_days == pytest.approx(2.0)         # (3+1)/2


def test_report_strategy_rsi2(session):
    _seed_two_strategies(session)
    r = report_strategy(session, "rsi2")
    assert r.signals_total == 3
    assert r.orders_submitted == 3
    assert r.positions_open == 1                          # TSLA still open
    assert r.positions_closed == 2
    assert r.num_wins == 2
    assert r.num_losses == 0
    assert r.realized_pnl == pytest.approx(150.0)         # 100 + 50
    assert r.win_rate == 1.0
    assert r.profit_factor == float("inf")                # all wins
    assert r.avg_hold_days == pytest.approx(2.5)          # (2+3)/2


@pytest.fixture()
def stub_global_config(monkeypatch):
    """Replace load_global_config so tests don't read the real production YAML."""
    from core import config as _cfg
    monkeypatch.setattr(_cfg, "load_global_config", lambda: {"strategies": []})


def test_report_all_strategies_returns_sorted_by_name(session, stub_global_config):
    _seed_two_strategies(session)
    reports = report_all_strategies(session)
    assert [r.strategy_name for r in reports] == ["pullback_ema", "rsi2"]


def test_report_empty_db_returns_empty_list(session, stub_global_config):
    """No DB activity AND empty config → no reports."""
    reports = report_all_strategies(session)
    assert reports == []


def test_report_includes_enabled_strategies_with_no_db_activity(session, monkeypatch):
    """A strategy enabled in YAML but with zero signals/positions should still
    appear in the report with all-zero numbers — so the dashboard surfaces
    newly-enabled strategies before they trade.
    """
    from core import config as _cfg
    monkeypatch.setattr(_cfg, "load_global_config", lambda: {
        "strategies": [
            {"name": "donchian", "enabled": True, "config_file": "donchian.yaml"},
            {"name": "disabled_strat", "enabled": False, "config_file": "x.yaml"},
        ],
    })
    reports = report_all_strategies(session)
    names = [r.strategy_name for r in reports]
    assert "donchian" in names
    assert "disabled_strat" not in names    # disabled YAML entries are skipped
    donchian = next(r for r in reports if r.strategy_name == "donchian")
    assert donchian.signals_total == 0
    assert donchian.orders_submitted == 0
    assert donchian.positions_closed == 0


def test_report_unions_db_and_yaml_sources(session, monkeypatch):
    """When DB has rsi2 activity AND yaml lists donchian, both should appear."""
    _seed_two_strategies(session)  # adds rsi2 + pullback_ema to DB
    from core import config as _cfg
    monkeypatch.setattr(_cfg, "load_global_config", lambda: {
        "strategies": [
            {"name": "donchian", "enabled": True, "config_file": "donchian.yaml"},
            {"name": "pullback_ema", "enabled": True, "config_file": "pullback_ema.yaml"},
        ],
    })
    names = [r.strategy_name for r in report_all_strategies(session)]
    assert sorted(names) == ["donchian", "pullback_ema", "rsi2"]


def test_report_strategy_zero_closed_positions(session):
    """A strategy with only open positions has 0 closed → no PnL stats."""
    run = repo.start_run(session, mode="paper", strategies=["pullback_ema"])
    sig = repo.record_signal(session, run,
        DomainSignal(symbol="AAPL", action=Action.BUY,
                     entry_price=100, stop_loss=98, take_profit=106,
                     strategy_name="pullback_ema"),
        bar_date=date(2026, 1, 5))[0]
    repo.record_order_submitted(session, sig, broker="alpaca", broker_order_id="a1",
        symbol="AAPL", side="buy", qty=10,
        entry_price=100, stop_loss=98, take_profit=106)
    repo.open_position(session, symbol="AAPL", strategy_name="pullback_ema",
        side="long", qty=10, avg_entry_price=100, stop_loss=98, take_profit=106)

    r = report_strategy(session, "pullback_ema")
    assert r.positions_open == 1
    assert r.positions_closed == 0
    assert r.realized_pnl == 0.0
    assert r.win_rate == 0.0
    assert r.avg_hold_days == 0.0
