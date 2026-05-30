"""Phase-3 gate: validate the PullbackEMA edge on a serious universe & window.

Runs a 5-year backtest of `pullback_ema` on the current S&P 500 (503 names)
using yfinance daily bars (cached on disk under ./data_cache/).

Usage:
    .venv/bin/python -m scripts.validate_edge
    .venv/bin/python -m scripts.validate_edge --start 2019-01-01 --end 2024-01-01
    .venv/bin/python -m scripts.validate_edge --limit 50          # smoke test

Caveats printed at the end of the report so we don't fool ourselves.
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

from rich.console import Console

from backtest.backtester import Backtester
from core.config import init, load_strategy_config
from core.registry import discover_strategies
from data.universe import load_sp500
from data.yfinance_provider import YFinanceProvider

console = Console()


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--start", default="2019-01-01")
    p.add_argument("--end", default="2024-01-01")
    p.add_argument("--capital", type=float, default=100_000.0)
    p.add_argument("--limit", type=int, default=0, help="Cap universe size for smoke tests")
    p.add_argument("--strategy", default="pullback_ema")
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args()

    settings, global_cfg = init()

    registry = discover_strategies()
    if args.strategy not in registry:
        console.print(f"[red]Unknown strategy {args.strategy}[/red]")
        return 1

    entry = next((s for s in global_cfg["strategies"] if s["name"] == args.strategy), None)
    cfg = load_strategy_config(entry["config_file"]) if entry and entry.get("config_file") else {}

    universe = load_sp500()
    if args.limit:
        universe = universe[: args.limit]
    cfg["universe"] = universe

    strat = registry[args.strategy](cfg)
    data_provider = YFinanceProvider(
        cache_dir=global_cfg.get("data", {}).get("cache_dir", "./data_cache")
    )

    console.print(
        f"[bold]Validate-edge run[/bold]: {args.strategy} | "
        f"{len(universe)} symbols | {args.start} → {args.end} | ${args.capital:,.0f}"
    )

    t0 = time.time()
    bt = Backtester(
        strategy=strat,
        data_provider=data_provider,
        starting_capital=args.capital,
        verbose=args.verbose,
    )
    result = bt.run(universe, start=args.start, end=args.end)
    elapsed = time.time() - t0

    console.print(f"\n[dim]Backtest finished in {elapsed:.1f}s[/dim]\n")
    console.print(f"[bold green]Performance Report — {args.strategy}[/bold green]")
    console.print(result.report.pretty())

    out_dir = Path("./backtest_results") / args.strategy
    out_dir.mkdir(parents=True, exist_ok=True)
    fname = f"{args.start}_to_{args.end}_sp500"
    if args.limit:
        fname += f"_n{args.limit}"
    result.equity_curve.to_csv(out_dir / f"{fname}_equity.csv", header=True)
    if result.trades:
        import pandas as pd
        pd.DataFrame([t.__dict__ for t in result.trades]).to_csv(
            out_dir / f"{fname}_trades.csv", index=False
        )
    console.print(f"\n[dim]Results saved to {out_dir}/[/dim]")

    # Verdict gate
    sharpe = result.report.sharpe
    console.print()
    if sharpe >= 1.0:
        console.print(f"[bold green]✓ Sharpe {sharpe:.2f} ≥ 1.0 — strong edge, proceed to Phase 3 (paper trade)[/bold green]")
    elif sharpe >= 0.8:
        console.print(f"[bold yellow]⚠ Sharpe {sharpe:.2f} in 0.8–1.0 — borderline; consider tuning before paper[/bold yellow]")
    else:
        console.print(f"[bold red]✗ Sharpe {sharpe:.2f} < 0.8 — strategy needs work before paper trading[/bold red]")

    console.print(
        "\n[dim]Caveats: (1) S&P 500 is a survivorship-biased universe — current members only. "
        "(2) No commissions/slippage modeled (Alpaca is commission-free; slippage on small caps real). "
        "(3) Same-day SL+TP triggers conservatively assume stop fires first.[/dim]"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
