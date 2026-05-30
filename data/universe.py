"""Universe loaders.

S&P 500 constituents are pulled from a public CSV mirror (datasets/s-and-p-500-companies)
and cached locally so we don't depend on network on every backtest run.

NOTE on survivorship bias: the list reflects *current* members. Backtests over
multi-year windows therefore overstate returns — losers (delisted, dropped from
index) are excluded by definition. Worth disclosing in any honest report.
"""

from __future__ import annotations

import urllib.request
from pathlib import Path

CACHE_FILE = Path(__file__).resolve().parent.parent / "data_cache" / "sp500.txt"
SOURCE_URL = (
    "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/"
    "master/data/constituents.csv"
)


def load_sp500(force_refresh: bool = False) -> list[str]:
    """Return S&P 500 ticker list, cached on disk.

    yfinance uses '-' rather than '.' in tickers (e.g. BRK-B not BRK.B).
    """
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    if force_refresh or not CACHE_FILE.exists():
        _download(CACHE_FILE)
    tickers = [
        line.strip().replace(".", "-")
        for line in CACHE_FILE.read_text().splitlines()
        if line.strip()
    ]
    return tickers


def _download(dest: Path) -> None:
    with urllib.request.urlopen(SOURCE_URL, timeout=30) as resp:
        raw = resp.read().decode("utf-8")
    # CSV: Symbol,Security,GICS Sector,...
    lines = raw.splitlines()
    if not lines or not lines[0].lower().startswith("symbol"):
        raise RuntimeError(f"unexpected CSV header from {SOURCE_URL}: {lines[0] if lines else ''!r}")
    tickers = [row.split(",", 1)[0].strip() for row in lines[1:] if row.strip()]
    dest.write_text("\n".join(tickers) + "\n")


if __name__ == "__main__":
    syms = load_sp500()
    print(f"Loaded {len(syms)} S&P 500 tickers (sample: {syms[:8]})")
