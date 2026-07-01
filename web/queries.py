"""Read-only DB queries the UI needs.

Centralized so that templates only consume already-computed view models —
no SQLAlchemy in the templates themselves. Tests target this module.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from sqlalchemy import select, func, desc
from sqlalchemy.orm import Session

from core.reporting import StrategyReport, report_all_strategies
from persistence.models import Order, Position, Run, Signal


# ----------------- view models -----------------
@dataclass
class HealthSummary:
    last_run_at: datetime | None
    last_run_finished_at: datetime | None
    last_run_mode: str | None
    last_run_signals: int
    last_run_orders: int
    last_run_error: str | None
    runs_last_7d: int
    hours_since_last_run: float | None


@dataclass
class OpenPositionRow:
    symbol: str
    strategy_name: str
    qty: int
    avg_entry_price: float
    stop_loss: float | None
    take_profit: float | None
    opened_at: datetime
    days_open: int
    # Live mark-to-market (None when the broker mark is unavailable).
    current_price: float | None = None
    market_value: float | None = None
    unrealized_pl: float | None = None
    unrealized_plpc: float | None = None


@dataclass
class AllocationRow:
    strategy_name: str
    cap_usd: float                 # capital_allocation_usd from config (0 = uncapped)
    invested: float                # sum(qty * avg_entry_price) of open positions (cost basis)
    market_value: float | None     # live sum of market_value (None if marks unavailable)
    n_open: int
    pct_used: float | None         # invested / cap (None when cap == 0)


@dataclass
class RunListRow:
    id: int
    started_at: datetime
    finished_at: datetime | None
    mode: str
    strategies_run: str
    signals_found: int
    orders_submitted: int
    orders_skipped: int
    orders_rejected: int
    error: str | None
    duration_secs: float | None


@dataclass
class SignalRow:
    id: int
    bar_date: datetime
    strategy_name: str
    symbol: str
    action: str
    entry_price: float
    stop_loss: float
    take_profit: float
    has_order: bool
    order_status: str | None


@dataclass
class EquityPoint:
    """One point on a daily equity curve. We don't store mark-to-market
    history yet; this is realized-PnL cumulative from closed positions."""
    day: date
    cumulative_pnl: float


@dataclass
class DailyPnl:
    day: date
    pnl: float
    n_trades: int


@dataclass
class StrategyChartData:
    strategy_name: str
    equity_curve: list[EquityPoint]
    daily_pnl: list[DailyPnl]
    win_loss_pnls: list[float]   # for histogram


@dataclass
class ChartsBundle:
    starting_capital: float
    per_strategy: list[StrategyChartData]
    combined_equity_curve: list[EquityPoint]
    signals_per_day: list[tuple[date, int]] = field(default_factory=list)


# ----------------- query functions -----------------
def health_summary(session: Session) -> HealthSummary:
    last = session.execute(select(Run).order_by(desc(Run.started_at)).limit(1)).scalar_one_or_none()
    week_ago = datetime.utcnow() - timedelta(days=7)
    runs_7d = int(session.execute(
        select(func.count(Run.id)).where(Run.started_at >= week_ago)
    ).scalar() or 0)
    hours_since = None
    if last is not None:
        hours_since = (datetime.utcnow() - last.started_at).total_seconds() / 3600.0
    return HealthSummary(
        last_run_at=last.started_at if last else None,
        last_run_finished_at=last.finished_at if last else None,
        last_run_mode=last.mode if last else None,
        last_run_signals=last.signals_found if last else 0,
        last_run_orders=last.orders_submitted if last else 0,
        last_run_error=last.error if last else None,
        runs_last_7d=runs_7d,
        hours_since_last_run=hours_since,
    )


def strategy_reports(session: Session) -> list[StrategyReport]:
    return report_all_strategies(session)


def open_positions(
    session: Session,
    marks: dict[str, dict[str, float]] | None = None,
) -> list[OpenPositionRow]:
    """Open positions, optionally enriched with live mark-to-market.

    ``marks`` is {symbol: {current_price, market_value, unrealized_pl,
    unrealized_plpc}} from broker.get_position_marks(). When omitted (or a
    symbol is missing — e.g. broker unreachable), the row's unrealized fields
    stay None and the dashboard renders them as "—". Pure DB read otherwise.
    """
    marks = marks or {}
    rows = session.execute(
        select(Position).where(Position.is_open == True).order_by(Position.opened_at.desc())  # noqa: E712
    ).scalars().all()
    out = []
    now = datetime.utcnow()
    for p in rows:
        days = (now - p.opened_at).days if p.opened_at else 0
        m = marks.get(p.symbol, {})
        out.append(OpenPositionRow(
            symbol=p.symbol, strategy_name=p.strategy_name, qty=p.qty,
            avg_entry_price=p.avg_entry_price,
            stop_loss=p.stop_loss, take_profit=p.take_profit,
            opened_at=p.opened_at, days_open=days,
            current_price=m.get("current_price"),
            market_value=m.get("market_value"),
            unrealized_pl=m.get("unrealized_pl"),
            unrealized_plpc=m.get("unrealized_plpc"),
        ))
    return out


def strategy_allocations(
    session: Session,
    caps: dict[str, float],
    marks: dict[str, dict[str, float]] | None = None,
) -> list[AllocationRow]:
    """Capital deployed per strategy vs its configured cap.

    ``caps`` maps strategy_name -> capital_allocation_usd (0 means uncapped).
    ``marks`` is the same broker mark map used by open_positions(); when a
    symbol's mark is missing, that position contributes nothing to
    market_value and the row's market_value falls back to None only if NO
    position in the strategy had a mark (so a partial-marks state still shows
    a partial live value rather than lying with a full one).

    Rows are emitted for every strategy in ``caps`` (so a flat strategy still
    shows its cap and $0 deployed), plus any strategy that has open positions
    but somehow isn't in caps (defensive — cap shown as 0/uncapped).
    """
    marks = marks or {}
    open_rows = session.execute(
        select(Position).where(Position.is_open == True)  # noqa: E712
    ).scalars().all()

    invested: dict[str, float] = {}
    mkt: dict[str, float] = {}
    marked_any: dict[str, bool] = {}
    n_open: dict[str, int] = {}
    for p in open_rows:
        invested[p.strategy_name] = invested.get(p.strategy_name, 0.0) + (p.qty * p.avg_entry_price)
        n_open[p.strategy_name] = n_open.get(p.strategy_name, 0) + 1
        m = marks.get(p.symbol)
        if m is not None and m.get("market_value") is not None:
            mkt[p.strategy_name] = mkt.get(p.strategy_name, 0.0) + float(m["market_value"])
            marked_any[p.strategy_name] = True

    names = list(caps.keys())
    for name in invested:
        if name not in caps:
            names.append(name)

    out: list[AllocationRow] = []
    for name in names:
        cap = float(caps.get(name, 0.0))
        inv = round(invested.get(name, 0.0), 2)
        mv = round(mkt[name], 2) if marked_any.get(name) else None
        pct = (inv / cap) if cap > 0 else None
        out.append(AllocationRow(
            strategy_name=name, cap_usd=cap, invested=inv,
            market_value=mv, n_open=n_open.get(name, 0), pct_used=pct,
        ))
    return out


def recent_runs(session: Session, limit: int = 50) -> list[RunListRow]:
    rows = session.execute(
        select(Run).order_by(desc(Run.started_at)).limit(limit)
    ).scalars().all()
    out = []
    for r in rows:
        dur = None
        if r.started_at and r.finished_at:
            dur = (r.finished_at - r.started_at).total_seconds()
        out.append(RunListRow(
            id=r.id, started_at=r.started_at, finished_at=r.finished_at,
            mode=r.mode, strategies_run=r.strategies_run,
            signals_found=r.signals_found, orders_submitted=r.orders_submitted,
            orders_skipped=r.orders_skipped, orders_rejected=r.orders_rejected,
            error=r.error, duration_secs=dur,
        ))
    return out


def signals_for_run(session: Session, run_id: int) -> list[SignalRow]:
    sigs = session.execute(
        select(Signal).where(Signal.run_id == run_id).order_by(Signal.strategy_name, Signal.symbol)
    ).scalars().all()
    out = []
    for s in sigs:
        # Look up the order status (if any non-rejected order)
        order = session.execute(
            select(Order).where(Order.signal_id == s.id, Order.status != "rejected").limit(1)
        ).scalar_one_or_none()
        out.append(SignalRow(
            id=s.id, bar_date=s.bar_date, strategy_name=s.strategy_name,
            symbol=s.symbol, action=s.action,
            entry_price=s.entry_price, stop_loss=s.stop_loss, take_profit=s.take_profit,
            has_order=order is not None,
            order_status=order.status if order else None,
        ))
    return out


def get_run(session: Session, run_id: int) -> Run | None:
    return session.execute(select(Run).where(Run.id == run_id)).scalar_one_or_none()


# ----------------- charts -----------------
def charts_bundle(session: Session, starting_capital: float = 100_000.0) -> ChartsBundle:
    """Compute everything needed for the analytics page in a single sweep."""
    closed = session.execute(
        select(Position).where(Position.is_open == False).order_by(Position.closed_at)  # noqa: E712
    ).scalars().all()

    by_strat: dict[str, list[Position]] = {}
    for p in closed:
        if not p.closed_at:
            continue
        by_strat.setdefault(p.strategy_name, []).append(p)

    per_strategy = []
    for name, positions in by_strat.items():
        # Daily P&L
        daily: dict[date, list[float]] = {}
        for p in positions:
            d = p.closed_at.date()
            daily.setdefault(d, []).append(p.realized_pnl or 0.0)
        daily_pnl_list = [
            DailyPnl(day=d, pnl=sum(pnls), n_trades=len(pnls))
            for d, pnls in sorted(daily.items())
        ]
        # Equity curve = cumulative realized
        equity = []
        running = 0.0
        for dp in daily_pnl_list:
            running += dp.pnl
            equity.append(EquityPoint(day=dp.day, cumulative_pnl=running))
        # Histogram raw values
        win_loss = [p.realized_pnl or 0.0 for p in positions]
        per_strategy.append(StrategyChartData(
            strategy_name=name,
            equity_curve=equity,
            daily_pnl=daily_pnl_list,
            win_loss_pnls=win_loss,
        ))

    # Combined equity curve = sum across strategies, day by day
    all_days: dict[date, float] = {}
    for sd in per_strategy:
        for dp in sd.daily_pnl:
            all_days[dp.day] = all_days.get(dp.day, 0.0) + dp.pnl
    combined = []
    running = 0.0
    for d in sorted(all_days):
        running += all_days[d]
        combined.append(EquityPoint(day=d, cumulative_pnl=running))

    # Signals per day (last 90 days)
    cutoff = datetime.utcnow() - timedelta(days=90)
    sig_rows = session.execute(
        select(Signal.bar_date, func.count(Signal.id))
        .where(Signal.bar_date >= cutoff)
        .group_by(Signal.bar_date)
        .order_by(Signal.bar_date)
    ).all()
    signals_per_day = [(r[0].date(), int(r[1])) for r in sig_rows]

    return ChartsBundle(
        starting_capital=starting_capital,
        per_strategy=per_strategy,
        combined_equity_curve=combined,
        signals_per_day=signals_per_day,
    )


# ----------------- config files -----------------
def list_config_files(config_dir) -> list[tuple[str, str]]:
    """Return a list of (display_name, full_relative_path) for every YAML
    file under config_dir. Used by the config viewer page.
    """
    from pathlib import Path
    out = []
    base = Path(config_dir)
    if not base.exists():
        return out
    for p in sorted(base.rglob("*.yaml")):
        rel = p.relative_to(base)
        out.append((str(rel), str(rel)))
    for p in sorted(base.rglob("*.yml")):
        rel = p.relative_to(base)
        out.append((str(rel), str(rel)))
    return out


def read_config_file(config_dir, rel_path: str) -> str:
    """Read a YAML file under config_dir, refusing path traversal attempts."""
    from pathlib import Path
    base = Path(config_dir).resolve()
    target = (base / rel_path).resolve()
    # Refuse anything escaping config/
    if not str(target).startswith(str(base) + "/") and target != base:
        raise PermissionError(f"path {rel_path!r} escapes config dir")
    if not target.is_file():
        raise FileNotFoundError(rel_path)
    if target.suffix not in (".yaml", ".yml"):
        raise PermissionError(f"only YAML files allowed: {rel_path!r}")
    return target.read_text(encoding="utf-8")
