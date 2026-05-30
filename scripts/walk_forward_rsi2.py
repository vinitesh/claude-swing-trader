"""Walk-forward sweep for the RSI(2) strategy.

Same train(2019-2021) → test(2022-2023) discipline as the PullbackEMA sweep,
with a smaller grid because RSI(2) has fewer knobs:

    oversold_threshold ∈ {5, 8, 10, 15}
    exit_threshold     ∈ {65, 70, 75}
    stop_loss_pct      ∈ {0.03, 0.04, 0.06}
    time_stop_days     ∈ {3, 5, 8}
    require_bull_regime∈ {False, True}

= 4 × 3 × 3 × 3 × 2 = 216 combos. Same in-memory data reuse pattern.
"""

from __future__ import annotations

import argparse
import itertools
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import pandas as pd
from rich.console import Console
from rich.table import Table

from backtest.backtester import Backtester
from data.universe import load_sp500
from data.yfinance_provider import YFinanceProvider
from scripts.walk_forward_sweep import InMemoryProvider
from strategies.rsi2 import RSI2

console = Console()


@dataclass
class ComboResult:
    oversold: int
    exit_thr: int
    stop: float
    time_stop: int
    regime: bool
    train_sharpe: float
    train_cagr: float
    train_trades: int
    test_sharpe: float = float("nan")
    test_cagr: float = float("nan")
    test_trades: int = 0
    test_max_dd: float = float("nan")


OVERSOLD = [5, 8, 10, 15]
EXIT_THR = [65, 70, 75]
STOPS = [0.03, 0.04, 0.06]
TIME_STOP = [3, 5, 8]
REGIMES = [False, True]


def _make_cfg(oversold, exit_thr, stop, time_stop, regime) -> dict:
    return {
        "allocation_pct": 0.20,
        "risk_pct": 0.01,
        "rsi_short_period": 2,
        "sma_long": 200,
        "oversold_threshold": oversold,
        "exit_threshold": exit_thr,
        "stop_loss_pct": stop,
        "take_profit_pct": 0.20,
        "time_stop_days": time_stop,
        "require_bull_regime": regime,
        "universe": [],
    }


def _run_one(cfg, universe, provider, start, end):
    cfg = {**cfg, "universe": universe}
    strat = RSI2(cfg)
    bt = Backtester(strategy=strat, data_provider=provider, starting_capital=100_000.0, verbose=False)
    res = bt.run(universe, start=start, end=end)
    r = res.report
    return r.sharpe, r.cagr, r.num_trades, r.max_drawdown_pct


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--train-start", default="2019-01-01")
    p.add_argument("--train-end", default="2021-12-31")
    p.add_argument("--test-start", default="2022-01-01")
    p.add_argument("--test-end", default="2023-12-31")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--top-k", type=int, default=15)
    args = p.parse_args()

    universe = load_sp500()
    if args.limit:
        universe = universe[: args.limit]

    yf = YFinanceProvider(cache_dir="./data_cache")
    bars: dict[str, pd.DataFrame] = {}
    t0 = time.time()
    for sym in universe:
        try:
            bars[sym] = yf.get_bars(sym, start=args.train_start, end=args.test_end)
        except Exception:
            continue
    universe = list(bars.keys())
    try:
        bars["SPY"] = yf.get_bars("SPY", start=args.train_start, end=args.test_end)
    except Exception as e:
        console.print(f"[red]SPY load failed: {e}[/red]")
        return 1
    provider = InMemoryProvider(bars)
    console.print(f"  loaded {len(universe)} symbols in {time.time()-t0:.1f}s")

    combos = list(itertools.product(OVERSOLD, EXIT_THR, STOPS, TIME_STOP, REGIMES))
    console.print(f"[bold]{len(combos)}[/bold] combos to evaluate on TRAIN {args.train_start}→{args.train_end}")

    results: list[ComboResult] = []
    t0 = time.time()
    for i, (over, ex, stop, ts, regime) in enumerate(combos, 1):
        cfg = _make_cfg(over, ex, stop, ts, regime)
        try:
            sh, cagr, n, _ = _run_one(cfg, universe, provider, args.train_start, args.train_end)
        except Exception as e:
            console.print(f"  ! combo {i} failed: {e}")
            continue
        results.append(ComboResult(
            oversold=over, exit_thr=ex, stop=stop, time_stop=ts, regime=regime,
            train_sharpe=sh, train_cagr=cagr, train_trades=n,
        ))
        if i % 20 == 0:
            elapsed = time.time() - t0
            eta = elapsed / i * (len(combos) - i)
            console.print(f"  [dim]{i}/{len(combos)} done in {elapsed:.0f}s — eta {eta:.0f}s[/dim]")

    console.print(f"\n[bold]Train sweep finished in {time.time()-t0:.1f}s[/bold]")

    results.sort(key=lambda r: r.train_sharpe, reverse=True)
    top = results[: args.top_k]
    console.print(f"\n[bold]Re-running top-{args.top_k} on TEST {args.test_start}→{args.test_end}...[/bold]")
    for r in top:
        cfg = _make_cfg(r.oversold, r.exit_thr, r.stop, r.time_stop, r.regime)
        try:
            sh, cagr, n, dd = _run_one(cfg, universe, provider, args.test_start, args.test_end)
            r.test_sharpe, r.test_cagr, r.test_trades, r.test_max_dd = sh, cagr, n, dd
        except Exception as e:
            console.print(f"  ! test failed: {e}")

    out_dir = Path("./backtest_results/rsi2")
    out_dir.mkdir(parents=True, exist_ok=True)
    pd.DataFrame([asdict(r) for r in results]).to_csv(out_dir / "walk_forward_sweep.csv", index=False)
    pd.DataFrame([asdict(r) for r in top]).to_csv(out_dir / "walk_forward_top.csv", index=False)

    tbl = Table(title=f"Top-{args.top_k} by train-Sharpe")
    for col in ("over", "exit", "stop", "tStop", "reg",
                "tr_Sh", "tr_CAGR", "tr_n", "TE_Sh", "te_CAGR", "te_n", "te_DD"):
        tbl.add_column(col)
    for r in top:
        tbl.add_row(
            str(r.oversold), str(r.exit_thr), f"{r.stop:.3f}",
            str(r.time_stop), "Y" if r.regime else "N",
            f"{r.train_sharpe:.2f}", f"{r.train_cagr*100:.1f}%", str(r.train_trades),
            f"{r.test_sharpe:.2f}", f"{r.test_cagr*100:.1f}%",
            str(r.test_trades), f"{r.test_max_dd*100:.1f}%",
        )
    console.print(tbl)

    test_sharpes = [r.test_sharpe for r in top if r.test_sharpe == r.test_sharpe]
    if not test_sharpes:
        console.print("[red]No valid test results.[/red]")
        return 1
    median_test = float(pd.Series(test_sharpes).median())
    n_pass = sum(s >= 0.8 for s in test_sharpes)
    avg_train = sum(r.train_sharpe for r in top) / len(top)
    avg_test = sum(test_sharpes) / len(test_sharpes)

    console.print()
    console.print(f"  Median test Sharpe across top-{args.top_k}: {median_test:.2f}")
    console.print(f"  Test Sharpe ≥ 0.8: {n_pass}/{len(test_sharpes)}")
    console.print(f"  Train→Test Sharpe drop: {avg_train:.2f} → {avg_test:.2f} ({avg_test-avg_train:+.2f})")

    if median_test >= 0.8 and n_pass >= len(test_sharpes) // 2:
        console.print("[bold green]✓ Robust edge — proceed to paper[/bold green]")
    elif median_test >= 0.5 and n_pass >= 2:
        console.print("[bold yellow]⚠ Partial edge — risk of selection bias[/bold yellow]")
    else:
        console.print("[bold red]✗ Edge does not survive — overfitting / no real alpha[/bold red]")

    console.print(f"\n[dim]Full results: {out_dir/'walk_forward_sweep.csv'}[/dim]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
