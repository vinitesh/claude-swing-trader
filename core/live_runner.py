"""Live runner: scans, sizes, gates, submits orders, and persists every step.

Responsibilities:
  1. Pre-flight (market open, account healthy, daily loss circuit breaker).
  2. For each enabled strategy: scan its universe, compute Signals.
  3. For each Signal: idempotency check (was this same signal already acted
     on today?), risk-gate, position size, broker submit.
  4. Record signal/order/position rows in SQLite.
  5. Send Telegram for each fill (or NullNotifier in dry-run).
  6. Emit a single summary alert at the end.

Idempotency contract:
  Re-running `swingbot run-live` for the same trading day is SAFE — already-
  submitted (strategy, symbol, bar_date) tuples are skipped, never duplicated.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from core.config import Settings
from core.registry import discover_strategies
from core.signal import Signal as DomainSignal
from core.strategy_base import Strategy
from data.alpaca_provider import AlpacaProvider
from data.provider_base import DataProvider
from data.yfinance_provider import YFinanceProvider
from execution.alpaca_broker import AlpacaBroker
from execution.broker_base import Broker
from notifications import build_notifier
from notifications.notifier_base import Notifier
from notifications.telegram_notifier import (
    fmt_error,
    fmt_order_failed,
    fmt_order_submitted,
    fmt_signal,
    fmt_summary,
)
from persistence import repository as repo
from persistence.db import session_scope
from risk.risk_manager import RiskLimits, RiskManager

log = logging.getLogger(__name__)


@dataclass
class RunOutcome:
    mode: str
    signals_found: int = 0
    orders_submitted: int = 0
    orders_skipped: int = 0
    orders_rejected: int = 0
    error: str | None = None


class LiveRunner:
    """Wires strategies → broker → DB → notifier for one daily run."""

    def __init__(
        self,
        settings: Settings,
        config: dict,
        broker: Broker | None = None,
        data_provider: DataProvider | None = None,
        notifier: Notifier | None = None,
        dry_run: bool = False,
    ):
        self.settings = settings
        self.config = config
        self.dry_run = dry_run
        self.mode = "dry-run" if dry_run else (settings.trading_mode or "paper")
        self.data: DataProvider = data_provider or self._build_data_provider()
        self.broker: Broker = broker or self._build_broker()
        self.notifier: Notifier = notifier or build_notifier(settings, config)
        self.risk = RiskManager(self._build_risk_limits())
        self.strategies: list[Strategy] = self._build_strategies()

    # --------------- Builders ---------------
    def _build_data_provider(self) -> DataProvider:
        primary = self.config.get("data", {}).get("primary", "yfinance")
        cache_dir = self.config.get("data", {}).get("cache_dir", "./data_cache")
        if primary == "alpaca":
            return AlpacaProvider(
                api_key=self.settings.alpaca_api_key,
                secret_key=self.settings.alpaca_secret_key,
                feed=self.settings.alpaca_data_feed,
            )
        return YFinanceProvider(cache_dir=cache_dir)

    def _build_broker(self) -> Broker:
        # Dry-run with no Alpaca creds: use the in-memory PaperBroker so the
        # full data + scan + persist + notify pipeline works without keys.
        if self.dry_run and not (
            self.settings.alpaca_api_key and self.settings.alpaca_secret_key
        ):
            from execution.paper_broker import PaperBroker
            log.info("dry-run + no Alpaca keys → using in-memory PaperBroker")
            return PaperBroker(starting_cash=100_000.0)
        is_paper = self.settings.trading_mode != "live"
        return AlpacaBroker(
            api_key=self.settings.alpaca_api_key,
            secret_key=self.settings.alpaca_secret_key,
            paper=is_paper,
        )

    def _build_risk_limits(self) -> RiskLimits:
        r = self.config.get("risk", {})
        return RiskLimits(
            max_open_positions=int(r.get("max_open_positions", 8)),
            max_per_strategy=int(r.get("max_per_strategy", 5)),
            daily_loss_limit_pct=float(r.get("daily_loss_limit_pct", 0.03)),
            min_position_size_usd=float(r.get("min_position_size_usd", 100)),
        )

    def _build_strategies(self) -> list[Strategy]:
        from core.config import load_strategy_config

        registry = discover_strategies()
        default_universe = self.config.get("universe", {}).get("default", [])
        out: list[Strategy] = []
        for entry in self.config.get("strategies", []):
            if not entry.get("enabled", True):
                continue
            name = entry["name"]
            if name not in registry:
                raise KeyError(
                    f"Strategy {name!r} not in registry. Available: {list(registry)}"
                )
            cfg = (
                load_strategy_config(entry["config_file"])
                if entry.get("config_file")
                else {}
            )
            if not cfg.get("universe"):
                cfg["universe"] = default_universe
            strat = registry[name](cfg)
            # Attach earnings calendar if strategy opted in. Single shared
            # instance across all strategies to amortize the cache.
            if int(cfg.get("avoid_earnings_within_days", 0)) > 0:
                if not hasattr(self, "_earnings_calendar"):
                    try:
                        from data.earnings import EarningsCalendar
                        self._earnings_calendar = EarningsCalendar()
                    except Exception as e:
                        log.warning("earnings calendar unavailable: %s", e)
                        self._earnings_calendar = None
                if self._earnings_calendar is not None:
                    strat.earnings_calendar = self._earnings_calendar
            out.append(strat)
            log.info("Loaded strategy: %s (%d symbols)", name, len(cfg["universe"]))
        return out

    # --------------- Pre-flight ---------------
    def _preflight(self) -> tuple[bool, str]:
        """Return (ok, reason). When ok=False, runner aborts before scanning."""
        if not self.dry_run and self.mode == "live":
            try:
                if not self.broker.is_market_open():
                    return False, "market is closed"
            except Exception as e:
                return False, f"is_market_open() failed: {e}"
        try:
            acct = self.broker.get_account()
        except Exception as e:
            return False, f"get_account() failed: {e}"
        if acct.equity <= 0:
            return False, f"account equity {acct.equity} non-positive"
        return True, ""

    # --------------- Run ---------------
    def run(self) -> RunOutcome:
        outcome = RunOutcome(mode=self.mode)
        ok, reason = self._preflight()
        if not ok:
            log.warning("preflight failed: %s", reason)
            outcome.error = f"preflight: {reason}"
            self.notifier.send(fmt_error("preflight", RuntimeError(reason)))
            return outcome

        today = date.today()

        with session_scope() as s:
            run_row = repo.start_run(
                s, mode=self.mode, strategies=[st.name for st in self.strategies]
            )
            try:
                self._scan_and_submit(s, run_row, today, outcome)
            except Exception as e:
                outcome.error = f"{type(e).__name__}: {e}"
                log.exception("run failed")
                self.notifier.send(fmt_error("run", e))
            finally:
                repo.finish_run(
                    s,
                    run_row,
                    signals_found=outcome.signals_found,
                    orders_submitted=outcome.orders_submitted,
                    orders_skipped=outcome.orders_skipped,
                    orders_rejected=outcome.orders_rejected,
                    error=outcome.error,
                )

            realized = repo.daily_pnl_realized(s, on_date=today)

        # Send summary outside the session
        self.notifier.send(
            fmt_summary(
                mode=self.mode,
                n_signals=outcome.signals_found,
                n_submitted=outcome.orders_submitted,
                n_skipped=outcome.orders_skipped,
                n_rejected=outcome.orders_rejected,
                realized_pnl=realized,
            )
        )
        return outcome

    # --------------- Capital accounting ---------------
    def _strategy_remaining_capital(self, strategy_name: str, session) -> float | None:
        """How many USD this strategy is still allowed to deploy.

        Returns None if the strategy has no per-strategy cap configured (cap=0).
        Otherwise: cap - sum(notional value of currently-open positions tagged
        to this strategy in our DB). Uses entry_price as a proxy for current
        notional (close-enough for sizing decisions; sync updates it).
        """
        strat = next((s for s in self.strategies if s.name == strategy_name), None)
        if strat is None or strat.capital_allocation_usd <= 0:
            return None
        from persistence import repository as _repo
        open_rows = [
            p for p in _repo.get_open_positions(session)
            if p.strategy_name == strategy_name
        ]
        deployed = sum((p.qty * p.avg_entry_price) for p in open_rows)
        return max(0.0, strat.capital_allocation_usd - deployed)

    # --------------- Scan + submit ---------------
    def _scan_and_submit(self, session, run_row, today: date, outcome: RunOutcome) -> None:
        end = today
        start = end - timedelta(days=400)  # ~250 trading days for SMA(200)

        for strat in self.strategies:
            for sym in strat.universe():
                try:
                    sig = self._scan_one(strat, sym, start, end)
                except Exception as e:
                    log.warning("scan %s/%s failed: %s", strat.name, sym, e)
                    continue
                if sig is None:
                    continue

                outcome.signals_found += 1
                # Persist signal (idempotent)
                sig_row, is_new = repo.record_signal(session, run_row, sig, bar_date=today)

                # Idempotency: already acted on this (strategy, symbol, day)?
                if not is_new and repo.signal_already_acted_today(
                    session, strat.name, sym, today
                ):
                    outcome.orders_skipped += 1
                    log.info("skip duplicate (already acted): %s/%s", strat.name, sym)
                    continue

                self._maybe_submit(session, sig, sig_row, outcome)

    def _scan_one(
        self, strat: Strategy, sym: str, start: date, end: date
    ) -> DomainSignal | None:
        df = self.data.get_bars(sym, start=start, end=end)
        df = strat.indicators(df)
        df.attrs["symbol"] = sym
        sig = strat.should_enter(df)
        return sig

    def _maybe_submit(
        self, session, sig: DomainSignal, sig_row, outcome: RunOutcome
    ) -> None:
        try:
            account = self.broker.get_account()
            open_positions = self.broker.get_positions()
        except Exception as e:
            log.warning("broker read failed for %s: %s", sig.symbol, e)
            outcome.orders_rejected += 1
            return

        # Position sizing (per-strategy logic in strategy_base)
        strat = next((s for s in self.strategies if s.name == sig.strategy_name), None)
        # If strategy has a per-strategy USD cap, size against the SMALLER of
        # (cash available, strategy capital remaining). This prevents one
        # strategy starving another in a shared account.
        strat_remaining = self._strategy_remaining_capital(sig.strategy_name, session)
        size_against = account.cash
        if strat is not None and strat.capital_allocation_usd > 0 and strat_remaining is not None:
            size_against = min(account.cash, strat_remaining)

        qty = (
            strat.position_size(
                account_value=size_against,
                entry_price=sig.entry_price,
                stop_loss=sig.stop_loss,
            )
            if strat
            else 0
        )

        ok, reason = self.risk.approve(
            signal=sig,
            account=account,
            open_positions=open_positions,
            qty=qty,
            strategy_remaining_capital=strat_remaining,
        )
        if not ok:
            outcome.orders_rejected += 1
            log.info("risk reject %s: %s", sig.symbol, reason)
            return

        # Send "signal found" alert
        self.notifier.send(fmt_signal(sig.strategy_name, sig.symbol, sig.entry_price, sig.stop_loss, sig.take_profit, qty=qty))

        if self.dry_run:
            repo.record_order_submitted(
                session, sig_row, broker="dry-run", broker_order_id=None,
                symbol=sig.symbol, side=sig.action.value.lower(), qty=qty,
                entry_price=sig.entry_price, stop_loss=sig.stop_loss,
                take_profit=sig.take_profit, status="dry-run",
            )
            outcome.orders_submitted += 1
            return

        # Real submission
        try:
            broker_oid = self.broker.submit_bracket_order(sig, qty)
        except Exception as e:
            log.exception("broker.submit failed for %s", sig.symbol)
            repo.record_order_submitted(
                session, sig_row, broker=self.broker.name, broker_order_id=None,
                symbol=sig.symbol, side=sig.action.value.lower(), qty=qty,
                entry_price=sig.entry_price, stop_loss=sig.stop_loss,
                take_profit=sig.take_profit, status="rejected",
                error=f"{type(e).__name__}: {e}",
            )
            outcome.orders_rejected += 1
            self.notifier.send(fmt_order_failed(sig.symbol, str(e)))
            return

        repo.record_order_submitted(
            session, sig_row, broker=self.broker.name, broker_order_id=broker_oid,
            symbol=sig.symbol, side=sig.action.value.lower(), qty=qty,
            entry_price=sig.entry_price, stop_loss=sig.stop_loss,
            take_profit=sig.take_profit, status="submitted",
        )
        # Track strategy↔symbol mapping for later sync (broker doesn't know).
        # We use entry_price as a placeholder for avg_entry_price; sync will update.
        repo.open_position(
            session,
            symbol=sig.symbol, strategy_name=sig.strategy_name, side="long",
            qty=qty, avg_entry_price=sig.entry_price,
            stop_loss=sig.stop_loss, take_profit=sig.take_profit,
        )
        outcome.orders_submitted += 1
        self.notifier.send(fmt_order_submitted(sig.symbol, qty, sig.entry_price, broker_oid))
