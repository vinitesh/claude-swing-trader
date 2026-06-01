"""CLI entry point.

Usage:
    swingbot backtest --strategy pullback_ema --start 2023-01-01 --end 2024-12-31
    swingbot scan --strategy pullback_ema
    swingbot list-strategies
"""

from __future__ import annotations

import sys
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from core.config import init, load_strategy_config
from core.logging_setup import logger, setup_logging
from core.registry import discover_strategies

console = Console()


@click.group()
def cli() -> None:
    """Swing trading platform CLI."""
    settings, _ = init()
    setup_logging(level=settings.log_level, log_dir=settings.log_dir)


@cli.command("list-strategies")
def list_strategies() -> None:
    """List all discovered strategies."""
    registry = discover_strategies()
    table = Table(title="Discovered strategies")
    table.add_column("Name", style="cyan")
    table.add_column("Class")
    table.add_column("Module")
    for name, cls in sorted(registry.items()):
        table.add_row(name, cls.__name__, cls.__module__)
    console.print(table)


@cli.command()
@click.option("--strategy", required=True, help="Strategy name (e.g. pullback_ema)")
@click.option("--start", required=True, help="Start date YYYY-MM-DD")
@click.option("--end", required=True, help="End date YYYY-MM-DD")
@click.option("--capital", default=100_000.0, type=float, help="Starting capital")
@click.option("--symbols", default="", help="Comma-separated override of universe")
@click.option("-v", "--verbose", is_flag=True)
def backtest(
    strategy: str,
    start: str,
    end: str,
    capital: float,
    symbols: str,
    verbose: bool,
) -> None:
    """Run a backtest for a single strategy."""
    settings, global_cfg = init()
    setup_logging(level=settings.log_level, log_dir=settings.log_dir)

    registry = discover_strategies()
    if strategy not in registry:
        console.print(f"[red]Unknown strategy: {strategy}[/red]")
        console.print(f"Available: {sorted(registry.keys())}")
        sys.exit(1)

    # Find this strategy's config_file from the global config
    entry = next((s for s in global_cfg["strategies"] if s["name"] == strategy), None)
    cfg = load_strategy_config(entry["config_file"]) if entry and entry.get("config_file") else {}

    # Universe
    if symbols:
        universe = [s.strip().upper() for s in symbols.split(",") if s.strip()]
    elif cfg.get("universe"):
        universe = cfg["universe"]
    else:
        universe = global_cfg["universe"]["default"]
    cfg["universe"] = universe

    strat = registry[strategy](cfg)

    # Data provider for backtest: yfinance is fastest
    from data.yfinance_provider import YFinanceProvider
    from backtest.backtester import Backtester

    data_provider = YFinanceProvider(cache_dir=global_cfg.get("data", {}).get("cache_dir", "./data_cache"))

    console.print(f"[bold]Backtesting[/bold] {strategy} on {len(universe)} symbols, {start} → {end}")
    bt = Backtester(
        strategy=strat,
        data_provider=data_provider,
        starting_capital=capital,
        verbose=verbose,
    )
    result = bt.run(universe, start=start, end=end)

    console.print()
    console.print(f"[bold green]Performance Report — {strategy}[/bold green]")
    console.print(result.report.pretty())

    # Persist results
    out_dir = Path("./backtest_results") / strategy
    out_dir.mkdir(parents=True, exist_ok=True)
    fname = f"{start}_to_{end}"
    result.equity_curve.to_csv(out_dir / f"{fname}_equity.csv", header=True)
    if result.trades:
        import pandas as pd
        pd.DataFrame([t.__dict__ for t in result.trades]).to_csv(out_dir / f"{fname}_trades.csv", index=False)
    console.print(f"\n[dim]Results saved to {out_dir}/[/dim]")


@cli.command()
@click.option("--strategy", default=None, help="Limit to one strategy (default: all enabled)")
def scan(strategy: str | None) -> None:
    """Run a live scan across all enabled strategies (no orders placed)."""
    from core.engine import TradingEngine
    settings, global_cfg = init()

    if strategy:
        global_cfg["strategies"] = [s for s in global_cfg["strategies"] if s["name"] == strategy]

    engine = TradingEngine(settings, global_cfg)
    signals = engine.scan()
    console.print(f"\n[bold]{len(signals)} signal(s) found[/bold]")


@cli.command("run-live")
@click.option("--dry-run", is_flag=True, help="Scan + risk-gate but do NOT submit orders")
@click.option("--strategy", default=None, help="Limit to one strategy (default: all enabled)")
def run_live(dry_run: bool, strategy: str | None) -> None:
    """Daily live run: scan + size + risk-gate + submit + persist + alert.

    Idempotent: re-running on the same trading day will not duplicate orders.
    """
    from core.live_runner import LiveRunner

    settings, global_cfg = init()
    setup_logging(level=settings.log_level, log_dir=settings.log_dir)

    if strategy:
        global_cfg["strategies"] = [
            s for s in global_cfg["strategies"] if s["name"] == strategy
        ]
        if not global_cfg["strategies"]:
            console.print(f"[red]Unknown strategy: {strategy}[/red]")
            sys.exit(1)

    runner = LiveRunner(settings=settings, config=global_cfg, dry_run=dry_run)
    outcome = runner.run()

    console.print()
    style = "green" if not outcome.error else "red"
    console.print(f"[bold {style}]Run complete — mode={outcome.mode}[/bold {style}]")
    console.print(f"  Signals found:     {outcome.signals_found}")
    console.print(f"  Orders submitted:  {outcome.orders_submitted}")
    console.print(f"  Skipped (dedupe):  {outcome.orders_skipped}")
    console.print(f"  Rejected (risk):   {outcome.orders_rejected}")
    if outcome.error:
        console.print(f"[red]  Error: {outcome.error}[/red]")
        sys.exit(2)


@cli.command("sync")
def sync() -> None:
    """Sync open positions/orders from Alpaca → SQLite. Updates fills, exits, P&L."""
    from core.live_runner import LiveRunner  # for broker
    from datetime import datetime as _dt

    settings, global_cfg = init()
    setup_logging(level=settings.log_level, log_dir=settings.log_dir)
    runner = LiveRunner(settings=settings, config=global_cfg, dry_run=False)
    broker = runner.broker

    from persistence import repository as repo
    from persistence.db import session_scope

    try:
        positions = broker.get_positions()
    except Exception as e:
        console.print(f"[red]broker.get_positions failed: {e}[/red]")
        sys.exit(2)

    open_symbols = {p.symbol for p in positions}
    console.print(f"[bold]Broker open positions:[/bold] {len(open_symbols)} — {sorted(open_symbols)}")

    closed = 0
    with session_scope() as s:
        # Any position open in DB but not at broker → mark closed.
        # We don't have exit price here; use last entry_price as a placeholder
        # (a proper sync would query Alpaca order history; this is the v1).
        db_positions = repo.get_open_positions(s)
        for p in db_positions:
            if p.symbol not in open_symbols:
                repo.close_position(
                    s, symbol=p.symbol, exit_price=p.avg_entry_price,
                    exit_reason="closed_at_broker", closed_at=_dt.utcnow(),
                )
                closed += 1
                console.print(f"  closed: {p.symbol} (broker no longer holds)")

    console.print(f"\n[dim]Closed {closed} position(s) in DB to match broker.[/dim]")
    console.print("[dim]Note: v1 sync uses entry_price as exit; for true P&L pull Alpaca order history.[/dim]")


@cli.command("report")
@click.option("--strategy", default=None, help="Limit to one strategy (default: all)")
@click.option("--json", "as_json", is_flag=True, help="Output JSON instead of pretty table")
def report(strategy: str | None, as_json: bool) -> None:
    """Per-strategy bake-off report: signals, orders, P&L, win rate, hold days."""
    from core.reporting import report_all_strategies, report_strategy
    from persistence.db import session_scope
    import json as _json

    settings, _ = init()
    setup_logging(level=settings.log_level, log_dir=settings.log_dir)

    with session_scope() as s:
        reports = (
            [report_strategy(s, strategy)] if strategy else report_all_strategies(s)
        )

    if as_json:
        console.print_json(_json.dumps([r.as_dict() for r in reports]))
        return

    if not reports:
        console.print("[yellow]No strategy data in DB yet. Run 'swingbot run-live' first.[/yellow]")
        return

    table = Table(title="Strategy Bake-Off Report")
    table.add_column("Strategy", style="cyan", no_wrap=True)
    table.add_column("Signals", justify="right")
    table.add_column("Orders", justify="right")
    table.add_column("Open", justify="right")
    table.add_column("Closed", justify="right")
    table.add_column("Realized P&L", justify="right")
    table.add_column("Win %", justify="right")
    table.add_column("Avg Win", justify="right")
    table.add_column("Avg Loss", justify="right")
    table.add_column("PF", justify="right")
    table.add_column("Hold (d)", justify="right")
    table.add_column("Last Run")

    total_pnl = 0.0
    total_signals = 0
    total_orders = 0
    for r in reports:
        total_pnl += r.realized_pnl
        total_signals += r.signals_total
        total_orders += r.orders_submitted
        pf_str = "∞" if r.profit_factor == float("inf") else f"{r.profit_factor:.2f}"
        pnl_color = "green" if r.realized_pnl >= 0 else "red"
        last_run = r.last_run_at.strftime("%Y-%m-%d %H:%M") if r.last_run_at else "—"
        table.add_row(
            r.strategy_name,
            str(r.signals_total),
            str(r.orders_submitted),
            str(r.positions_open),
            str(r.positions_closed),
            f"[{pnl_color}]${r.realized_pnl:+,.2f}[/{pnl_color}]",
            f"{r.win_rate*100:.1f}%" if r.positions_closed else "—",
            f"${r.avg_win:.2f}" if r.num_wins else "—",
            f"${r.avg_loss:.2f}" if r.num_losses else "—",
            pf_str if r.positions_closed else "—",
            f"{r.avg_hold_days:.1f}" if r.avg_hold_days else "—",
            last_run,
        )

    if len(reports) > 1:
        table.add_section()
        pnl_color = "green" if total_pnl >= 0 else "red"
        table.add_row(
            "[bold]TOTAL[/bold]",
            str(total_signals), str(total_orders), "—", "—",
            f"[{pnl_color}]${total_pnl:+,.2f}[/{pnl_color}]",
            "—", "—", "—", "—", "—", "—",
        )

    console.print(table)


@cli.command("warm-earnings")
@click.option("--force", is_flag=True, help="Refetch even if cache is fresh")
@click.option(
    "--sleep", "sleep_secs", default=0.5, type=float,
    help="Seconds between yfinance calls (politeness)",
)
def warm_earnings(force: bool, sleep_secs: float) -> None:
    """Pre-warm the earnings calendar cache for all enabled-strategy universes.

    Designed for nightly cron at 2 AM ET. Walks the union of universes from
    every strategy whose YAML opts into the earnings filter, refetches each
    symbol's earnings dates, and sleeps briefly between calls to stay below
    yfinance soft rate limits. Trading runs the next day will hit warm cache.
    """
    from data.earnings import EarningsCalendar

    settings, global_cfg = init()
    setup_logging(level=settings.log_level, log_dir=settings.log_dir)

    # Union of universes across strategies that opt into the earnings filter.
    # Strategies with avoid_earnings_within_days=0 are skipped — no point.
    syms: set[str] = set()
    default_universe = global_cfg.get("universe", {}).get("default", []) or []
    for entry in global_cfg.get("strategies", []):
        if not entry.get("enabled", True):
            continue
        cfg = (
            load_strategy_config(entry["config_file"])
            if entry.get("config_file")
            else {}
        )
        if int(cfg.get("avoid_earnings_within_days", 0)) <= 0:
            continue
        universe = cfg.get("universe") or default_universe
        syms.update(universe)

    if not syms:
        console.print("[yellow]No strategies opt into the earnings filter — nothing to warm.[/yellow]")
        return

    console.print(
        f"[bold]Warming earnings cache[/bold] for {len(syms)} symbols "
        f"(force={force}, sleep={sleep_secs}s)"
    )
    cal = EarningsCalendar()
    statuses = cal.refresh(sorted(syms), force=force, sleep_secs=sleep_secs)

    fresh = sum(1 for s in statuses.values() if s == "fresh")
    refreshed = sum(1 for s in statuses.values() if s == "refreshed")
    failed = sum(1 for s in statuses.values() if s == "failed")
    console.print(
        f"[green]Done.[/green] fresh={fresh} refreshed={refreshed} failed={failed}"
    )


@cli.command("serve")
@click.option("--host", default=None, help="Override host (default: WEB_HOST or 0.0.0.0)")
@click.option("--port", default=None, type=int, help="Override port (default: WEB_PORT or 8082)")
def serve(host: str | None, port: int | None) -> None:
    """Run the read-only web UI (FastAPI + uvicorn)."""
    import uvicorn
    settings, _ = init()
    bind_host = host or settings.web_host
    bind_port = port or settings.web_port
    if not (settings.web_password or "").strip():
        console.print("[yellow]⚠ WEB_PASSWORD is empty in .env — every protected route will return 503.[/yellow]")
    console.print(f"[bold]Starting web UI on {bind_host}:{bind_port}[/bold]")
    uvicorn.run("web.main:app", host=bind_host, port=bind_port, log_level=settings.log_level.lower())


if __name__ == "__main__":
    cli()
