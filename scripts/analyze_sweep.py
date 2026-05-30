"""Post-hoc analysis of walk_forward_sweep.csv.

Answers:
  1. Are top-K-on-train params robust on test?
  2. Is there a CLUSTER of nearby params that all do well?
     (Robust edges show as smooth surfaces; lucky picks are isolated peaks.)
  3. Where does each parameter's marginal effect land?

Usage:
    .venv/bin/python -m scripts.analyze_sweep
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
from rich.console import Console
from rich.table import Table

console = Console()
RES_PATH = Path("./backtest_results/pullback_ema/walk_forward_sweep.csv")


def main() -> int:
    if not RES_PATH.exists():
        console.print(f"[red]No sweep file at {RES_PATH}. Run scripts.walk_forward_sweep first.[/red]")
        return 1
    df = pd.read_csv(RES_PATH)
    console.print(f"Loaded {len(df)} combo results from {RES_PATH}\n")

    # 1. Train vs test correlation — robust edge => positive correlation
    valid = df.dropna(subset=["test_sharpe"])
    if len(valid) > 5:
        corr = valid["train_sharpe"].corr(valid["test_sharpe"])
        console.print(f"[bold]Train↔Test Sharpe correlation:[/bold] {corr:.3f}")
        console.print("  >0.5 = robust signal, 0.2–0.5 = noisy edge, <0.2 = effectively lottery\n")

    # 2. Marginal effects — for each param, average test Sharpe by value
    for param in ("ema_fast", "rsi_lo", "stop", "rr", "regime"):
        agg = (
            valid.groupby(param)["test_sharpe"]
            .agg(["mean", "median", "count"])
            .round(3)
        )
        console.print(f"[bold]Test Sharpe by {param}:[/bold]")
        console.print(agg.to_string())
        console.print()

    # 3. Top by train, with test
    top = df.sort_values("train_sharpe", ascending=False).head(20)
    tbl = Table(title="Top 20 by train-Sharpe")
    cols = ["ema_fast", "rsi_lo", "rsi_hi", "stop", "rr", "regime",
            "train_sharpe", "train_cagr", "train_trades",
            "test_sharpe", "test_cagr", "test_trades", "test_max_dd"]
    for c in cols:
        tbl.add_column(c)
    for _, r in top.iterrows():
        tbl.add_row(*[
            f"{r[c]:.3f}" if isinstance(r[c], float) and abs(r[c]) < 100 else str(r[c])
            for c in cols
        ])
    console.print(tbl)

    # 4. Best test-Sharpe (post-hoc, NOT a pickable strategy — selection bias)
    console.print("\n[bold]Top 5 by TEST sharpe (peeking — not selectable in real life):[/bold]")
    best_test = df.sort_values("test_sharpe", ascending=False).head(5)
    print(best_test[cols].to_string(index=False))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
