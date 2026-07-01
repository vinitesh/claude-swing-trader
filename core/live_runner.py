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


@dataclass
class ExitOutcome:
    mode: str
    positions_checked: int = 0
    stops_raised: int = 0
    signal_exits: int = 0
    time_stops: int = 0
    errors: int = 0
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
                # Fail-soft: log + alert instead of crashing the entire run.
                # Common cause: YAML enables a strategy whose Python module
                # hasn't been deployed (image build skew). Other strategies
                # should still get to trade.
                msg = (
                    f"Strategy {name!r} enabled in YAML but not found in registry "
                    f"(available: {list(registry)}). Skipping it; deploy the module "
                    f"and rebuild the image to enable."
                )
                log.error(msg)
                # Best-effort alert — don't let a notifier failure also crash us.
                try:
                    from notifications.telegram_notifier import fmt_error
                    self.notifier.send(fmt_error("strategy-load", RuntimeError(msg)))
                except Exception:
                    pass
                continue
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

    # --------------- Exit management ---------------
    def manage_exits(self) -> ExitOutcome:
        """Apply indicator/time-driven exits to currently-open positions.

        Mirrors the backtester's per-bar exit pass (core.exits), so live exits
        the same way the strategy was validated:
          1. Trailing-stop ratchet → raise the resting Alpaca stop leg.
          2. Signal exit (e.g. RSI(2) > 70) → market close at today's price.
          3. Time stop (held >= time_stop_days) → market close.
        Fixed SL/TP are left to the Alpaca bracket legs (filled broker-side).

        Idempotent and safe to re-run: it only acts on positions still open in
        the DB, and closing one removes it from the next pass.
        """
        from core.exits import compute_trailing_stop, indicator_exit_reason

        outcome = ExitOutcome(mode=self.mode)
        ok, reason = self._preflight()
        if not ok:
            outcome.error = f"preflight: {reason}"
            log.warning("manage_exits preflight failed: %s", reason)
            return outcome

        today = date.today()
        end = today
        start = end - timedelta(days=400)
        strat_by_name = {s.name: s for s in self.strategies}

        # Snapshot what the broker actually holds, so we only manage live
        # positions (skip ones whose exit already filled — sync closes those).
        try:
            held_symbols = {p.symbol for p in self.broker.get_positions()}
        except Exception as e:
            outcome.error = f"get_positions failed: {e}"
            log.warning("manage_exits aborting: %s", e)
            return outcome

        with session_scope() as s:
            open_rows = repo.get_open_positions(s)
            for row in open_rows:
                strat = strat_by_name.get(row.strategy_name)
                if strat is None:
                    # Strategy disabled/removed — leave its positions alone.
                    continue
                outcome.positions_checked += 1
                try:
                    self._manage_one_exit(
                        s, row, strat, today, start, end, outcome, held_symbols,
                        compute_trailing_stop, indicator_exit_reason,
                    )
                except Exception as e:
                    outcome.errors += 1
                    log.exception("manage_exits failed for %s/%s", row.strategy_name, row.symbol)

        self.notifier.send(
            fmt_summary(
                mode=f"exits:{self.mode}",
                n_signals=outcome.positions_checked,
                n_submitted=outcome.signal_exits + outcome.time_stops,
                n_skipped=outcome.stops_raised,
                n_rejected=outcome.errors,
                realized_pnl=0.0,
            )
        )
        return outcome

    def _manage_one_exit(
        self, session, row, strat, today, start, end, outcome, held_symbols,
        compute_trailing_stop, indicator_exit_reason,
    ) -> None:
        # Only act on positions the broker ACTUALLY still holds. If a prior
        # exit already filled (broker flat) we skip — sync closes the DB row.
        # This prevents re-submitting a market close on an already-exited symbol.
        if row.symbol not in held_symbols:
            return

        # Build the indicator window for this symbol (same shape as scan).
        df = self.data.get_bars(row.symbol, start=start, end=end)
        df = strat.indicators(df)
        df.attrs["symbol"] = row.symbol

        # Domain Position from the DB row (carries opened_at + current stop).
        from core.signal import Position as DomainPosition
        pos = DomainPosition(
            symbol=row.symbol, qty=row.qty, avg_entry_price=row.avg_entry_price,
            side=row.side, strategy_name=row.strategy_name, opened_at=row.opened_at,
            stop_loss=row.stop_loss, take_profit=row.take_profit,
        )

        # 1. Trailing-stop ratchet: raise the resting broker stop leg. Broker is
        # the source of truth for the stop; mirror into the DB row only on success.
        new_stop = compute_trailing_stop(strat, pos, df)
        if new_stop is not None:
            try:
                if self.broker.update_stop_price(row.symbol, new_stop):
                    row.stop_loss = float(new_stop)
                    outcome.stops_raised += 1
                    log.info("trailing stop raised %s -> %.2f", row.symbol, new_stop)
            except Exception as e:
                outcome.errors += 1
                log.warning("update_stop_price failed for %s: %s", row.symbol, e)

        # 2 & 3. Signal exit / time stop. We do NOT close the DB row here — sync
        # is the single close-path authority and records the REAL fill price
        # (once the market order settles; if that's after-hours it reconciles on
        # the next sync, not necessarily the same day). Here we only: cancel the
        # resting bracket legs (so a stop/TP can't fill after our market sell and
        # flip us short), then submit the market close. dry_run places no orders.
        reason = indicator_exit_reason(strat, pos, df, today)
        if reason is None:
            return

        if self.dry_run:
            log.info("[dry-run] would %s exit %s", reason, row.symbol)
        else:
            try:
                # Abort the close unless EVERY bracket leg was canceled — a
                # surviving leg could fill after our sell and flip us short.
                if not self.broker.cancel_orders_for_symbol(row.symbol):
                    outcome.errors += 1
                    log.warning(
                        "skipping %s exit for %s: not all bracket legs canceled",
                        reason, row.symbol,
                    )
                    return
                self.broker.close_position(row.symbol)
            except Exception as e:
                outcome.errors += 1
                log.warning("exit close failed for %s: %s", row.symbol, e)
                return

        if reason == "signal_exit":
            outcome.signal_exits += 1
        else:
            outcome.time_stops += 1
        log.info("%s exit submitted for %s (DB close deferred to sync)", reason, row.symbol)

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
        # Refuse to stack a second open DB row on a symbol we already hold. The
        # broker nets everything in a symbol into ONE position, so a duplicate
        # open row is inherently unsyncable and previously wedged `sync`
        # (MultipleResultsFound). Treat as a risk rejection — no order sent.
        if repo.has_open_position(session, sig.symbol):
            outcome.orders_rejected += 1
            log.info("skip duplicate entry (already hold %s): %s", sig.symbol, sig.strategy_name)
            return

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
