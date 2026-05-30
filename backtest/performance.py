"""Performance metrics for a list of completed trades + an equity curve."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime

import numpy as np
import pandas as pd


@dataclass
class Trade:
    symbol: str
    strategy_name: str
    entry_date: datetime
    exit_date: datetime
    entry_price: float
    exit_price: float
    qty: int
    pnl: float
    exit_reason: str           # "take_profit" | "stop_loss" | "time_stop" | "eod"
    metadata: dict = field(default_factory=dict)

    @property
    def return_pct(self) -> float:
        return (self.exit_price - self.entry_price) / self.entry_price if self.entry_price else 0.0

    @property
    def hold_days(self) -> int:
        return (self.exit_date.date() - self.entry_date.date()).days


@dataclass
class PerformanceReport:
    starting_capital: float
    ending_equity: float
    total_pnl: float
    total_return_pct: float
    cagr: float
    sharpe: float
    sortino: float
    max_drawdown_pct: float
    win_rate: float
    profit_factor: float
    avg_win: float
    avg_loss: float
    avg_hold_days: float
    num_trades: int
    num_wins: int
    num_losses: int
    exit_reasons: dict[str, int]

    def pretty(self) -> str:
        rows = [
            ("Starting capital",    f"${self.starting_capital:,.2f}"),
            ("Ending equity",       f"${self.ending_equity:,.2f}"),
            ("Total PnL",           f"${self.total_pnl:,.2f}"),
            ("Total return",        f"{self.total_return_pct*100:.2f}%"),
            ("CAGR",                f"{self.cagr*100:.2f}%"),
            ("Sharpe",              f"{self.sharpe:.2f}"),
            ("Sortino",             f"{self.sortino:.2f}"),
            ("Max drawdown",        f"{self.max_drawdown_pct*100:.2f}%"),
            ("Trades",              str(self.num_trades)),
            ("Win rate",            f"{self.win_rate*100:.1f}%"),
            ("Profit factor",       f"{self.profit_factor:.2f}"),
            ("Avg win",             f"${self.avg_win:.2f}"),
            ("Avg loss",            f"${self.avg_loss:.2f}"),
            ("Avg hold (days)",     f"{self.avg_hold_days:.1f}"),
            ("Exit reasons",        ", ".join(f"{k}={v}" for k, v in self.exit_reasons.items())),
        ]
        width = max(len(k) for k, _ in rows)
        return "\n".join(f"  {k:<{width}}  {v}" for k, v in rows)


def compute_performance(
    starting_capital: float,
    equity_curve: pd.Series,
    trades: list[Trade],
) -> PerformanceReport:
    if equity_curve.empty:
        ending = starting_capital
    else:
        ending = float(equity_curve.iloc[-1])

    total_pnl = ending - starting_capital
    total_ret = total_pnl / starting_capital if starting_capital else 0.0

    # CAGR
    if equity_curve.empty or len(equity_curve) < 2:
        cagr = 0.0
    else:
        days = max((equity_curve.index[-1] - equity_curve.index[0]).days, 1)
        years = days / 365.25
        cagr = (ending / starting_capital) ** (1 / years) - 1 if years > 0 and starting_capital > 0 else 0.0

    # Sharpe & Sortino on daily equity returns
    sharpe = sortino = 0.0
    if len(equity_curve) > 5:
        daily_ret = equity_curve.pct_change().dropna()
        if not daily_ret.empty and daily_ret.std() > 0:
            sharpe = float(daily_ret.mean() / daily_ret.std() * math.sqrt(252))
        downside = daily_ret[daily_ret < 0]
        if not downside.empty and downside.std() > 0:
            sortino = float(daily_ret.mean() / downside.std() * math.sqrt(252))

    # Max drawdown
    if equity_curve.empty:
        max_dd = 0.0
    else:
        running_max = equity_curve.cummax()
        drawdown = (equity_curve - running_max) / running_max
        max_dd = float(drawdown.min()) if not drawdown.empty else 0.0

    wins = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl <= 0]
    win_rate = len(wins) / len(trades) if trades else 0.0
    gross_profit = sum(t.pnl for t in wins)
    gross_loss = -sum(t.pnl for t in losses)
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (float("inf") if gross_profit > 0 else 0.0)
    avg_win = (gross_profit / len(wins)) if wins else 0.0
    avg_loss = (-gross_loss / len(losses)) if losses else 0.0
    avg_hold = float(np.mean([t.hold_days for t in trades])) if trades else 0.0
    exit_reasons: dict[str, int] = {}
    for t in trades:
        exit_reasons[t.exit_reason] = exit_reasons.get(t.exit_reason, 0) + 1

    return PerformanceReport(
        starting_capital=starting_capital,
        ending_equity=ending,
        total_pnl=total_pnl,
        total_return_pct=total_ret,
        cagr=cagr,
        sharpe=sharpe,
        sortino=sortino,
        max_drawdown_pct=max_dd,
        win_rate=win_rate,
        profit_factor=profit_factor,
        avg_win=avg_win,
        avg_loss=avg_loss,
        avg_hold_days=avg_hold,
        num_trades=len(trades),
        num_wins=len(wins),
        num_losses=len(losses),
        exit_reasons=exit_reasons,
    )
