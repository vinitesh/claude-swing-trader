"""Event-driven backtester.

Replays daily bars through the same Strategy.should_enter() that runs in
production. Bracket exits (stop-loss, take-profit) are simulated by
checking next-bar high/low against the levels.

Key simplifications:
  - Daily bars only.
  - Entries fill at next-day open (avoid look-ahead).
  - Exits: SL/TP triggered intraday — fill at the trigger price.
  - No slippage, no commissions (Alpaca is free for stocks).
  - Capital allocation per signal capped by strategy.allocation_pct.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Iterable

import pandas as pd

from backtest.performance import PerformanceReport, Trade, compute_performance
from core.regime import RegimeFilter
from core.signal import Action, Position, Signal
from core.strategy_base import Strategy
from data.provider_base import DataProvider
from execution.paper_broker import PaperBroker
from risk.risk_manager import RiskLimits, RiskManager


@dataclass
class BacktestResult:
    report: PerformanceReport
    equity_curve: pd.Series
    trades: list[Trade]


class Backtester:
    def __init__(
        self,
        strategy: Strategy,
        data_provider: DataProvider,
        starting_capital: float = 100_000.0,
        risk_limits: RiskLimits | None = None,
        verbose: bool = False,
    ):
        self.strategy = strategy
        self.data_provider = data_provider
        self.starting_capital = float(starting_capital)
        self.risk = RiskManager(risk_limits)
        self.verbose = verbose

    # --------------------------- Public API ---------------------------
    def run(
        self,
        symbols: Iterable[str],
        start: str | date,
        end: str | date,
    ) -> BacktestResult:
        # 0. Build regime filter (SPY 200-SMA bull/bear) and attach to strategy.
        # Strategies that ignore it pay no cost; PullbackEMA opts in via config.
        try:
            regime = RegimeFilter(self.data_provider, start=start, end=end)
            self.strategy.regime_filter = regime
        except Exception as e:
            print(f"  ! regime filter unavailable, proceeding without: {e}")

        # 1. Load + prep data for every symbol
        bars: dict[str, pd.DataFrame] = {}
        for sym in symbols:
            try:
                df = self.data_provider.get_bars(sym, start=start, end=end)
                df = self.strategy.indicators(df)
                df.attrs["symbol"] = sym
                bars[sym] = df
            except Exception as e:
                # Always surface — silent skip masks real bugs.
                print(f"  ! skipping {sym}: {type(e).__name__}: {e}")

        if not bars:
            raise RuntimeError("No data loaded for any symbol")

        # 2. Build the union of trading dates across all symbols
        all_dates = sorted({d for df in bars.values() for d in df.index})

        broker = PaperBroker(starting_cash=self.starting_capital)
        trades: list[Trade] = []
        equity_points: list[tuple[pd.Timestamp, float]] = []
        time_stop_days = getattr(self.strategy, "time_stop_days", lambda: 999)()

        # 3. Walk forward day-by-day
        for i, today in enumerate(all_dates):
            today_ts = pd.Timestamp(today)

            # 3a. Process exits FIRST (existing positions resolve before new entries)
            for sym, pos in list(broker.get_positions_dict().items()):
                df = bars.get(sym)
                if df is None or today_ts not in df.index:
                    continue
                bar = df.loc[today_ts]

                # Signal-based exit (e.g. RSI(2) > 70). Checked before bracket
                # to give strategies a chance to take profit/loss on indicators
                # rather than only on price-trigger levels.
                window = df.loc[:today_ts]
                if self.strategy.should_exit_signal(pos, window):
                    exit_price = float(bar["close"])
                    broker.close_position(sym, exit_price=exit_price)
                    trades.append(_make_trade(pos, today_ts, exit_price, "signal_exit"))
                    continue

                exit_info = self._check_bracket_exit(pos, bar)
                if exit_info is None:
                    # Time stop?
                    opened_date = pos.opened_at.date() if isinstance(pos.opened_at, datetime) else pos.opened_at
                    today_date = today_ts.date()
                    held = (today_date - opened_date).days
                    if held >= time_stop_days:
                        exit_price = float(bar["close"])
                        broker.close_position(sym, exit_price=exit_price)
                        trades.append(_make_trade(pos, today_ts, exit_price, "time_stop"))
                else:
                    exit_price, reason = exit_info
                    broker.close_position(sym, exit_price=exit_price)
                    trades.append(_make_trade(pos, today_ts, exit_price, reason))

            # 3b. Look for new entries. Re-fetch account snapshot after each
            # entry — cash depletes within the day as positions open.
            for sym, df in bars.items():
                if today_ts not in df.index:
                    continue
                window = df.loc[:today_ts]
                signal = self.strategy.should_enter(window)
                if signal is None:
                    continue

                account = broker.get_account()  # refresh: cash changes mid-day
                # Size against cash (not equity) so we don't over-allocate
                qty = self.strategy.position_size(
                    account_value=account.cash,
                    entry_price=signal.entry_price,
                    stop_loss=signal.stop_loss,
                )
                positions = broker.get_positions()
                ok, reason = self.risk.approve(
                    signal=signal,
                    account=account,
                    open_positions=positions,
                    qty=qty,
                )
                if not ok:
                    if self.verbose:
                        print(f"  [{today}] reject {sym}: {reason}")
                    continue

                # Stamp signal with the simulated date so trades record correct hold time
                signal.timestamp = today_ts.to_pydatetime()
                broker.submit_bracket_order(signal, qty)
                if self.verbose:
                    print(f"  [{today}] OPEN {sym} qty={qty} entry={signal.entry_price:.2f} stop={signal.stop_loss:.2f} tp={signal.take_profit:.2f}")

            # 3c. Mark-to-market & record equity
            today_prices = {
                sym: float(df.loc[today_ts, "close"])
                for sym, df in bars.items()
                if today_ts in df.index
            }
            broker.mark_to_market(today_prices)
            equity_points.append((today_ts, broker.get_account().equity))

        # 4. Close all remaining positions at last available price
        last_date = pd.Timestamp(all_dates[-1])
        for sym, pos in list(broker.get_positions_dict().items()):
            df = bars.get(sym)
            if df is None or df.empty:
                continue
            exit_price = float(df["close"].iloc[-1])
            broker.close_position(sym, exit_price=exit_price)
            trades.append(_make_trade(pos, last_date, exit_price, "eod"))

        equity_curve = pd.Series(
            data=[p[1] for p in equity_points],
            index=pd.DatetimeIndex([p[0] for p in equity_points]),
            name="equity",
        )
        report = compute_performance(self.starting_capital, equity_curve, trades)
        return BacktestResult(report=report, equity_curve=equity_curve, trades=trades)

    # --------------------------- Helpers ---------------------------
    @staticmethod
    def _check_bracket_exit(pos: Position, bar: pd.Series) -> tuple[float, str] | None:
        """If the day's range crosses SL or TP, return (fill_price, reason)."""
        high = float(bar["high"])
        low = float(bar["low"])

        # Long position
        if pos.side == "long":
            sl_hit = pos.stop_loss is not None and low <= pos.stop_loss
            tp_hit = pos.take_profit is not None and high >= pos.take_profit
            # If both could trigger same day, conservative assumption: stop fires first.
            if sl_hit:
                return float(pos.stop_loss), "stop_loss"
            if tp_hit:
                return float(pos.take_profit), "take_profit"
        else:  # short
            sl_hit = pos.stop_loss is not None and high >= pos.stop_loss
            tp_hit = pos.take_profit is not None and low <= pos.take_profit
            if sl_hit:
                return float(pos.stop_loss), "stop_loss"
            if tp_hit:
                return float(pos.take_profit), "take_profit"
        return None


def _make_trade(pos: Position, exit_date: pd.Timestamp, exit_price: float, reason: str) -> Trade:
    sign = 1 if pos.side == "long" else -1
    pnl = sign * (exit_price - pos.avg_entry_price) * pos.qty
    return Trade(
        symbol=pos.symbol,
        strategy_name=pos.strategy_name,
        entry_date=pos.opened_at if isinstance(pos.opened_at, datetime) else datetime.combine(pos.opened_at, datetime.min.time()),
        exit_date=exit_date.to_pydatetime(),
        entry_price=pos.avg_entry_price,
        exit_price=exit_price,
        qty=pos.qty,
        pnl=pnl,
        exit_reason=reason,
    )
