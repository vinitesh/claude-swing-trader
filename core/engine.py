"""TradingEngine: wires strategies, data, broker, and risk together for live runs.

For backtesting, use backtest.backtester.Backtester directly — it shares
the same Strategy interface but runs deterministically on historical data.
"""

from __future__ import annotations

from typing import Any

from core.config import Settings, load_strategy_config
from core.logging_setup import logger
from core.registry import discover_strategies
from core.strategy_base import Strategy
from data.alpaca_provider import AlpacaProvider
from data.provider_base import DataProvider
from data.yfinance_provider import YFinanceProvider
from execution.alpaca_broker import AlpacaBroker
from execution.broker_base import Broker
from risk.risk_manager import RiskLimits, RiskManager


class TradingEngine:
    def __init__(
        self,
        settings: Settings,
        config: dict[str, Any],
        broker: Broker | None = None,
        data_provider: DataProvider | None = None,
    ):
        self.settings = settings
        self.config = config
        self.data: DataProvider = data_provider or self._build_data_provider()
        self.broker: Broker = broker or self._build_broker()
        self.risk = RiskManager(self._build_risk_limits())
        self.strategies: list[Strategy] = self._build_strategies()

    # --------------- Builders ---------------
    def _build_data_provider(self) -> DataProvider:
        primary = self.config.get("data", {}).get("primary", "yfinance")
        cache_dir = self.config.get("data", {}).get("cache_dir", "./data_cache")
        if primary == "alpaca":
            return AlpacaProvider(
                api_key=self.settings.alpaca_api_key,
                secret_key=self.settings.alpaca_secret_key,
                feed=self.settings.alpaca_data_feed,
            )
        return YFinanceProvider(cache_dir=cache_dir)

    def _build_broker(self) -> Broker:
        broker_type = self.config.get("broker", {}).get("type", "alpaca")
        if broker_type != "alpaca":
            raise ValueError(f"Unknown broker type: {broker_type}")
        is_paper = self.settings.trading_mode != "live"
        return AlpacaBroker(
            api_key=self.settings.alpaca_api_key,
            secret_key=self.settings.alpaca_secret_key,
            paper=is_paper,
        )

    def _build_risk_limits(self) -> RiskLimits:
        r = self.config.get("risk", {})
        return RiskLimits(
            max_open_positions=int(r.get("max_open_positions", 8)),
            max_per_strategy=int(r.get("max_per_strategy", 5)),
            daily_loss_limit_pct=float(r.get("daily_loss_limit_pct", 0.03)),
            min_position_size_usd=float(r.get("min_position_size_usd", 100)),
        )

    def _build_strategies(self) -> list[Strategy]:
        registry = discover_strategies()
        default_universe = self.config.get("universe", {}).get("default", [])
        out: list[Strategy] = []
        for entry in self.config.get("strategies", []):
            if not entry.get("enabled", True):
                continue
            name = entry["name"]
            if name not in registry:
                raise KeyError(f"Strategy {name!r} not found in registry. Available: {list(registry)}")
            strat_cfg = load_strategy_config(entry["config_file"]) if entry.get("config_file") else {}
            # Inherit universe if strategy didn't define one
            if not strat_cfg.get("universe"):
                strat_cfg["universe"] = default_universe
            out.append(registry[name](strat_cfg))
            logger.info(f"Loaded strategy: {name} (universe={len(strat_cfg['universe'])} symbols)")
        return out

    # --------------- Run modes ---------------
    def scan(self) -> list:
        """Run a one-shot scan across all enabled strategies. Returns Signals (no orders submitted)."""
        from datetime import date, timedelta

        signals = []
        end = date.today()
        start = end - timedelta(days=400)  # ~250 trading days, enough for 200 SMA

        for strategy in self.strategies:
            for sym in strategy.universe():
                try:
                    df = self.data.get_bars(sym, start=start, end=end)
                    df = strategy.indicators(df)
                    df.attrs["symbol"] = sym
                    sig = strategy.should_enter(df)
                    if sig is not None:
                        signals.append(sig)
                        logger.info(f"  📈 {strategy.name}: {sym} entry={sig.entry_price:.2f} stop={sig.stop_loss:.2f} tp={sig.take_profit:.2f}")
                except Exception as e:
                    logger.warning(f"  ! {sym}: {e}")
        return signals
