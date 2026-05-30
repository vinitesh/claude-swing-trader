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


if __name__ == "__main__":
    cli()
