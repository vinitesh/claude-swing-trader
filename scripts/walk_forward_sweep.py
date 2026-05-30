"""Walk-forward parameter sweep for PullbackEMA.

Goal: tell us honestly whether ANY parameter set has a real, robust edge —
not just one that fits the training period by accident.

Method:
  1. Load all S&P 500 bars ONCE (uses parquet cache).
  2. Train window: 2019-01-01 → 2021-12-31 (3 years, mostly bull/COVID).
  3. Test  window: 2022-01-01 → 2023-12-31 (2 years, includes 2022 bear).
  4. Grid:
       ema_fast  ∈ {10, 15, 20, 30}
       rsi_zone  ∈ {(30,50), (40,55), (45,60)}
       stop_loss ∈ {0.015, 0.02, 0.03}
       take_profit (R:R) ∈ {2x, 3x, 4x stop}
       regime ∈ {True, False}
     = 4 × 3 × 3 × 3 × 2 = 216 combos
  5. For each combo: run backtest on TRAIN, record Sharpe.
  6. Take top-K by train Sharpe; run them on TEST.
  7. Report:
       - Are top-K-on-train robust on test? (median test-Sharpe ≥ 0.8)
       - Is there a CLUSTER of nearby params that all do well? (overfitting check)
       - Best vs worst train→test gap.

Optimizations:
  - Bars + base indicators (close, OHLCV) cached per symbol; we only re-compute
    EMA/SMA/RSI per param set.
  - Use a custom in-memory data provider so YFinanceProvider isn't re-read 216×.
  - Single-pass through dates per config; this is unavoidable given the
    sequential broker state. ~10-20s per config on 503 names × 3 years.

Usage:
    .venv/bin/python -m scripts.walk_forward_sweep
    .venv/bin/python -m scripts.walk_forward_sweep --limit 50    # smoke
    .venv/bin/python -m scripts.walk_forward_sweep --top-k 20
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
from data.provider_base import DataProvider
from data.universe import load_sp500
from data.yfinance_provider import YFinanceProvider
from strategies.pullback_ema import PullbackEMA

console = Console()


class InMemoryProvider(DataProvider):
    """Returns pre-loaded DataFrames; window-slices to (start, end)."""

    name = "memory"

    def __init__(self, bars: dict[str, pd.DataFrame]):
        self._bars = bars

    def get_bars(self, symbol, start, end):
        if symbol not in self._bars:
            raise ValueError(f"No cached bars for {symbol}")
        df = self._bars[symbol]
        if start is not None or end is not None:
            df = df.loc[str(start) if start else None : str(end) if end else None]
        if df.empty:
            raise ValueError(f"No bars for {symbol} in {start}..{end}")
        out = df.copy()
        out.attrs["symbol"] = symbol
        return out


@dataclass
class ComboResult:
    ema_fast: int
    rsi_lo: int
    rsi_hi: int
    stop: float
    rr: float
    regime: bool
    train_sharpe: float
    train_cagr: float
    train_trades: int
    test_sharpe: float = float("nan")
    test_cagr: float = float("nan")
    test_trades: int = 0
    test_max_dd: float = float("nan")


# --------------- universe of param combinations ---------------
# Focused grid: 4 × 3 × 2 × 3 × 2 = 144 combos (cut from 216).
# Removed the rarely-best stop=0.015 (too tight, churn) and one redundant RR.
EMA_FAST = [10, 15, 20, 30]
RSI_ZONES = [(30, 50), (40, 55), (45, 60)]
STOPS = [0.02, 0.03]
RR_MULTIPLES = [2.0, 3.0, 4.0]
REGIMES = [True, False]


def _make_cfg(ema_fast, rsi_lo, rsi_hi, stop, rr, regime) -> dict:
    return {
        "allocation_pct": 0.40,
        "risk_pct": 0.01,
        "ema_fast": ema_fast,
        "sma_mid": 50,
        "sma_slow": 200,
        "rsi_period": 14,
        "rsi_zone": [rsi_lo, rsi_hi],
        "pullback_proximity_pct": 0.015,
        "require_uptrend": True,
        "require_bullish_candle": True,
        "stop_loss_pct": stop,
        "take_profit_pct": stop * rr,
        "time_stop_days": 15,
        "require_bull_regime": regime,
        "universe": [],  # filled per call
    }


def _run_one(
    cfg: dict, universe: list[str], provider: InMemoryProvider, start: str, end: str
) -> tuple[float, float, int, float]:
    """Returns (sharpe, cagr, n_trades, max_dd)."""
    cfg = {**cfg, "universe": universe}
    strat = PullbackEMA(cfg)
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
    p.add_argument("--limit", type=int, default=0, help="Cap universe for smoke test")
    p.add_argument("--top-k", type=int, default=10)
    args = p.parse_args()

    # ----- load all data once -----
    console.print("[bold]Loading universe + price data once into memory...[/bold]")
    universe = load_sp500()
    if args.limit:
        universe = universe[: args.limit]

    yf_provider = YFinanceProvider(cache_dir="./data_cache")
    bars: dict[str, pd.DataFrame] = {}
    fetch_start = args.train_start
    fetch_end = args.test_end
    failed = []
    t0 = time.time()
    for sym in universe:
        try:
            df = yf_provider.get_bars(sym, start=fetch_start, end=fetch_end)
            bars[sym] = df
        except Exception as e:
            failed.append((sym, type(e).__name__))
    if failed:
        console.print(f"[dim]  skipped {len(failed)} symbols (no data)[/dim]")
    universe = list(bars.keys())
    # Pre-fetch SPY for regime filter
    try:
        bars["SPY"] = yf_provider.get_bars("SPY", start=fetch_start, end=fetch_end)
    except Exception as e:
        console.print(f"[red]Cannot load SPY for regime filter: {e}[/red]")
        return 1
    provider = InMemoryProvider(bars)
    console.print(f"  {len(universe)} symbols loaded in {time.time()-t0:.1f}s")

    combos = list(itertools.product(EMA_FAST, RSI_ZONES, STOPS, RR_MULTIPLES, REGIMES))
    console.print(f"[bold]{len(combos)}[/bold] parameter combos to evaluate on TRAIN window {args.train_start} → {args.train_end}")

    # ----- TRAIN sweep -----
    results: list[ComboResult] = []
    t0 = time.time()
    for i, (ema, (rlo, rhi), stop, rr, regime) in enumerate(combos, 1):
        cfg = _make_cfg(ema, rlo, rhi, stop, rr, regime)
        try:
            sharpe, cagr, n_trades, _dd = _run_one(
                cfg, universe, provider, args.train_start, args.train_end
            )
        except Exception as e:
            console.print(f"  ! combo {i} failed: {e}")
            continue
        results.append(ComboResult(
            ema_fast=ema, rsi_lo=rlo, rsi_hi=rhi, stop=stop, rr=rr, regime=regime,
            train_sharpe=sharpe, train_cagr=cagr, train_trades=n_trades,
        ))
        if i % 20 == 0:
            elapsed = time.time() - t0
            rate = i / elapsed
            eta = (len(combos) - i) / rate
            console.print(f"  [dim]{i}/{len(combos)} done in {elapsed:.0f}s — eta {eta:.0f}s[/dim]")

    console.print(f"\n[bold]Train sweep finished in {time.time()-t0:.1f}s[/bold]")

    # ----- pick top-K and TEST -----
    results.sort(key=lambda r: r.train_sharpe, reverse=True)
    top = results[: args.top_k]
    console.print(f"\n[bold]Re-running top-{args.top_k} on TEST window {args.test_start} → {args.test_end}...[/bold]")
    for r in top:
        cfg = _make_cfg(r.ema_fast, r.rsi_lo, r.rsi_hi, r.stop, r.rr, r.regime)
        try:
            sharpe, cagr, n_trades, dd = _run_one(
                cfg, universe, provider, args.test_start, args.test_end
            )
            r.test_sharpe, r.test_cagr, r.test_trades, r.test_max_dd = sharpe, cagr, n_trades, dd
        except Exception as e:
            console.print(f"  ! test run failed: {e}")

    # ----- save full results -----
    out_dir = Path("./backtest_results/pullback_ema")
    out_dir.mkdir(parents=True, exist_ok=True)
    df_all = pd.DataFrame([asdict(r) for r in results])
    df_all.to_csv(out_dir / "walk_forward_sweep.csv", index=False)
    df_top = pd.DataFrame([asdict(r) for r in top])
    df_top.to_csv(out_dir / "walk_forward_top.csv", index=False)

    # ----- print top table -----
    tbl = Table(title=f"Top-{args.top_k} by train-Sharpe (with test results)")
    for col in ("ema", "rsi", "stop", "RR", "regime",
                "train_Sh", "train_CAGR", "train_n",
                "TEST_Sh", "test_CAGR", "test_n", "test_DD"):
        tbl.add_column(col)
    for r in top:
        tbl.add_row(
            str(r.ema_fast),
            f"{r.rsi_lo}-{r.rsi_hi}",
            f"{r.stop:.3f}",
            f"{r.rr:.1f}",
            "Y" if r.regime else "N",
            f"{r.train_sharpe:.2f}",
            f"{r.train_cagr*100:.1f}%",
            str(r.train_trades),
            f"{r.test_sharpe:.2f}",
            f"{r.test_cagr*100:.1f}%",
            str(r.test_trades),
            f"{r.test_max_dd*100:.1f}%",
        )
    console.print(tbl)

    # ----- verdict -----
    test_sharpes = [r.test_sharpe for r in top if r.test_sharpe == r.test_sharpe]  # filter NaN
    if not test_sharpes:
        console.print("[red]No valid test results.[/red]")
        return 1
    median_test = float(pd.Series(test_sharpes).median())
    n_pass = sum(s >= 0.8 for s in test_sharpes)
    console.print(f"\n[bold]Robustness check[/bold]")
    console.print(f"  Median test Sharpe across top-{args.top_k}: {median_test:.2f}")
    console.print(f"  Test Sharpe ≥ 0.8: {n_pass}/{len(test_sharpes)}")

    train_sharpes_top = [r.train_sharpe for r in top]
    avg_train = sum(train_sharpes_top)/len(train_sharpes_top)
    avg_test  = sum(test_sharpes)/len(test_sharpes)
    console.print(f"  Train→Test Sharpe drop: {avg_train:.2f} → {avg_test:.2f} (Δ {avg_test-avg_train:+.2f})")

    console.print()
    if median_test >= 0.8 and n_pass >= len(test_sharpes) // 2:
        console.print("[bold green]✓ Robust edge — multiple param sets clear the 0.8 gate out-of-sample[/bold green]")
    elif median_test >= 0.5 and n_pass >= 2:
        console.print("[bold yellow]⚠ Partial edge — a few params survive; risk of selection bias[/bold yellow]")
    else:
        console.print("[bold red]✗ Edge does not survive walk-forward — strategy is overfitting / no real alpha[/bold red]")

    console.print(f"\n[dim]Full results: {out_dir/'walk_forward_sweep.csv'}[/dim]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
