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

    # A DB position is only "gone" if the broker neither HOLDS it nor has a
    # WORKING order for it. Without the working-order check, an after-close
    # run-live (orders queued, not yet filled) would be falsely closed here —
    # the bug that desynced the DB and over-deployed capital. See get_open_order_symbols.
    try:
        pending_symbols = broker.get_open_order_symbols()
    except Exception as e:
        # Fail SAFE: if we can't confirm pending orders, do NOT close anything,
        # rather than risk falsely closing positions whose entry is still queued.
        console.print(f"[red]broker.get_open_order_symbols failed: {e} — aborting sync to avoid false closes.[/red]")
        sys.exit(2)

    held_symbols = {p.symbol for p in positions}
    alive_symbols = held_symbols | pending_symbols
    console.print(
        f"[bold]Broker:[/bold] {len(held_symbols)} held, "
        f"{len(pending_symbols)} with working orders "
        f"({len(alive_symbols)} alive total)"
    )

    closed = 0
    with session_scope() as s:
        db_positions = repo.get_open_positions(s)
        buckets = repo.classify_for_sync(
            [p.symbol for p in db_positions], held_symbols, pending_symbols
        )
        to_close = set(buckets["close"])
        # A symbol can have >1 open DB row (a strategy re-entered a symbol it
        # already held). The broker NETS these into ONE position with ONE exit
        # fill, so we must NOT stamp that single fill onto every duplicate row —
        # that would double-count realized P&L. Only the FIRST (oldest) row we
        # close for a symbol gets the real fill; any further duplicate rows are
        # closed at $0 under a distinct reason, since no separate exit exists.
        symbols_already_filled: set[str] = set()
        for p in db_positions:
            if p.symbol not in to_close:
                continue
            # Genuinely gone from the broker → close, using the REAL exit fill
            # price when we can find one for THIS position (true P&L). If there's
            # no matching filled sell, the symbol left the broker without a real
            # exit — most likely an entry order that was canceled/expired and
            # never actually opened — so we record a 0-P&L placeholder under a
            # distinct reason rather than fabricating a round-trip.
            if p.symbol in symbols_already_filled:
                # Duplicate row for a symbol whose single real exit we already
                # attributed to the oldest row. Close flat, don't re-count.
                fill = None
                dup = True
            else:
                try:
                    fill = broker.get_last_exit_fill(p.symbol, opened_after=p.opened_at)
                except Exception:
                    fill = None
                dup = False
            if fill is not None:
                exit_price, _ = fill
                exit_reason = "exit_filled"
                tag = f"real fill @ {exit_price:.2f}"
                symbols_already_filled.add(p.symbol)
            elif dup:
                exit_price = p.avg_entry_price
                exit_reason = "dup_no_separate_exit"
                tag = f"duplicate row — broker netted; flat @ {exit_price:.2f}"
            else:
                exit_price = p.avg_entry_price
                exit_reason = "no_exit_fill"
                tag = f"no matching exit fill — placeholder @ {exit_price:.2f}"
            repo.close_position(
                s, symbol=p.symbol, exit_price=exit_price,
                exit_reason=exit_reason, closed_at=_dt.utcnow(),
                position_id=p.id,
            )
            closed += 1
            console.print(f"  closed: {p.symbol} ({tag})")

    console.print(
        f"\n[dim]Closed {closed} position(s); left "
        f"{len(buckets['keep_held'])} held + {len(buckets['keep_pending'])} "
        f"with pending orders untouched.[/dim]"
    )


@cli.command("manage-exits")
@click.option("--dry-run", is_flag=True, help="Evaluate exits but place no broker orders.")
def manage_exits(dry_run: bool) -> None:
    """Apply indicator/time exits (trailing stop, RSI/signal exit, time stop).

    Run shortly AFTER run-live so exits evaluate on the same final daily close
    the backtest uses. Fixed stop-loss/take-profit are handled by the Alpaca
    bracket legs; this covers the indicator-driven exits the broker can't know.
    """
    from core.live_runner import LiveRunner

    settings, global_cfg = init()
    setup_logging(level=settings.log_level, log_dir=settings.log_dir)
    runner = LiveRunner(settings=settings, config=global_cfg, dry_run=dry_run)
    outcome = runner.manage_exits()

    console.print()
    style = "green" if not outcome.error else "red"
    console.print(f"[bold {style}]Exit pass complete — mode={outcome.mode}[/bold {style}]")
    console.print(f"  Positions checked: {outcome.positions_checked}")
    console.print(f"  Stops raised:      {outcome.stops_raised}")
    console.print(f"  Signal exits:      {outcome.signal_exits}")
    console.print(f"  Time stops:        {outcome.time_stops}")
    console.print(f"  Errors:            {outcome.errors}")
    if outcome.error:
        console.print(f"[red]  Error: {outcome.error}[/red]")
        sys.exit(2)


@cli.command("backfill-exits")
@click.option("--yes", is_flag=True, help="Apply updates. Without it, dry-run only (no writes).")
def backfill_exits(yes: bool) -> None:
    """Recover true exit prices/P&L for closed positions recorded at $0.

    Earlier a tz-comparison bug made exit-fill lookup fail, so genuinely-closed
    positions were saved with exit_reason no_exit_fill / closed_at_broker and
    realized_pnl=0. This re-queries Alpaca for the sell that filled between each
    position's entry and recorded close, and rewrites exit_price + realized_pnl.
    Dry-run by default; --yes writes.
    """
    from core.live_runner import LiveRunner
    from persistence.db import session_scope
    from persistence.models import Position
    from sqlalchemy import select

    settings, global_cfg = init()
    setup_logging(level=settings.log_level, log_dir=settings.log_dir)
    runner = LiveRunner(settings=settings, config=global_cfg, dry_run=False)
    broker = runner.broker

    STALE_REASONS = ("no_exit_fill", "closed_at_broker")
    updated = recovered = skipped = ambiguous = 0
    total_pnl_delta = 0.0
    with session_scope() as s:
        rows = s.execute(
            select(Position).where(Position.is_open == False)  # noqa: E712
        ).scalars().all()
        candidates = [p for p in rows if p.exit_reason in STALE_REASONS]
        console.print(f"[bold]{len(candidates)} closed position(s) with placeholder exits.[/bold]")
        for p in candidates:
            try:
                # qty-match scopes to THIS position's exit; window bounds the time.
                fills = broker.find_exit_fills_in_window(
                    p.symbol, p.opened_at, p.closed_at, qty=p.qty
                )
            except Exception as e:
                console.print(f"  [yellow]{p.symbol}: lookup failed: {e}[/yellow]")
                continue
            if not fills:
                skipped += 1
                continue  # no matching filled sell — leave as-is (never filled)
            if len(fills) > 1:
                # Ambiguous (symbol re-traded; multiple qty-matching sells in
                # window). Do NOT guess — flag for manual review.
                ambiguous += 1
                console.print(
                    f"  [yellow]{p.symbol}: {len(fills)} matching sells in window "
                    f"— SKIPPED (ambiguous, needs manual review)[/yellow]"
                )
                continue
            exit_price, _, _ = fills[0]
            sign = 1 if p.side == "long" else -1
            new_pnl = sign * (exit_price - p.avg_entry_price) * p.qty
            recovered += 1
            total_pnl_delta += new_pnl
            console.print(
                f"  {p.strategy_name:12} {p.symbol:6} entry={p.avg_entry_price:.2f} "
                f"exit ${exit_price:.2f}  P&L ${new_pnl:+.2f}"
            )
            if yes:
                p.exit_price = exit_price
                p.realized_pnl = new_pnl
                p.exit_reason = "exit_filled_backfill"
                updated += 1
        if not yes:
            s.rollback()

    console.print(
        f"\n[bold]{'Applied' if yes else 'DRY-RUN'}:[/bold] {recovered} recoverable, "
        f"{updated} written, {ambiguous} ambiguous (skipped), {skipped} no-fill. "
        f"Net P&L recovered ${total_pnl_delta:+.2f}"
    )
    if not yes:
        console.print("[yellow]Re-run with --yes to write these changes.[/yellow]")


@cli.command("close-all")
@click.option("--yes", is_flag=True, help="Required confirmation flag — without it, dry-run only.")
def close_all(yes: bool) -> None:
    """Flatten ALL open broker positions, then mark them closed in the DB.

    Liquidates every open position at the broker (paper or live, per .env),
    then reconciles SQLite so the DB matches the now-empty broker. Without
    --yes this only lists what WOULD be closed (dry-run), placing no orders.
    """
    from core.live_runner import LiveRunner
    from datetime import datetime as _dt

    settings, global_cfg = init()
    setup_logging(level=settings.log_level, log_dir=settings.log_dir)

    # Hard guard: this command is destructive. Refuse to run against a real-money
    # account unless explicitly forced — the platform is meant to be paper-only.
    if (settings.trading_mode or "").lower() == "live":
        console.print(
            "[red]Refusing to run close-all against a LIVE account "
            "(TRADING_MODE=live). This command is paper-only by design.[/red]"
        )
        sys.exit(2)

    runner = LiveRunner(settings=settings, config=global_cfg, dry_run=False)
    broker = runner.broker

    from persistence import repository as repo
    from persistence.db import session_scope

    try:
        positions = broker.get_positions()
    except Exception as e:
        console.print(f"[red]broker.get_positions failed: {e}[/red]")
        sys.exit(2)

    symbols = sorted({p.symbol for p in positions})
    console.print(f"[bold]Broker open positions:[/bold] {len(symbols)} — {symbols}")

    if not symbols:
        console.print("[green]Nothing to close — broker is already flat.[/green]")
        return

    if not yes:
        console.print(
            "[yellow]Dry-run: pass --yes to actually liquidate these positions.[/yellow]"
        )
        return

    # 1) Cancel ALL open orders first — including resting bracket stop-loss /
    # take-profit legs. If we skipped this, a child order could fill after the
    # liquidation below and silently re-open a position.
    try:
        n_cancelled = broker.cancel_all_orders()
        console.print(f"[dim]Cancelled {n_cancelled} open order(s) (incl. bracket legs).[/dim]")
    except Exception as e:
        console.print(f"[red]cancel_all_orders failed: {e} — aborting before liquidation.[/red]")
        sys.exit(2)

    # 2) Liquidate each position. Track which symbols were SUCCESSFULLY closed
    # so we only reconcile those in the DB.
    closed_ok: set[str] = set()
    failed: list[str] = []
    for sym in symbols:
        try:
            broker.close_position(sym)
            closed_ok.add(sym)
            console.print(f"  submitted liquidation: {sym}")
        except Exception as e:
            failed.append(sym)
            console.print(f"  [red]FAILED to close {sym}: {e}[/red]")

    # 3) Reconcile DB. Only mark a DB position closed if its broker liquidation
    # succeeded (or the broker no longer reports it). A symbol that FAILED to
    # close stays open in the DB so it remains visible / managed — never make a
    # still-held position invisible. Exit price unknown (async market exit) — use
    # entry_price placeholder, same convention as `sync`; a later sync corrects P&L.
    closed_db = 0
    skipped_db: list[str] = []
    with session_scope() as s:
        for p in repo.get_open_positions(s):
            if p.symbol in failed:
                skipped_db.append(p.symbol)
                continue
            repo.close_position(
                s, symbol=p.symbol, exit_price=p.avg_entry_price,
                exit_reason="close_all", closed_at=_dt.utcnow(),
                position_id=p.id,
            )
            closed_db += 1

    console.print(
        f"\n[bold]Done.[/bold] Submitted liquidation for {len(closed_ok)}/{len(symbols)} "
        f"at broker, reconciled {closed_db} DB position(s)."
    )
    console.print(
        "[dim]Note: liquidations are async market orders — 'submitted' ≠ filled. "
        "Re-run `swingbot sync` or `report` shortly to confirm the broker is flat.[/dim]"
    )
    if skipped_db:
        console.print(f"[yellow]Left open in DB (broker close failed): {skipped_db}[/yellow]")
    if failed:
        console.print(f"[red]Failed at broker: {failed} — re-run close-all or check manually.[/red]")
        sys.exit(2)


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
