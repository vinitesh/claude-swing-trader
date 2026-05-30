# Swing Trading Platform

Multi-strategy swing trading platform for US stocks built on Alpaca.
Designed to be extensible — adding a new strategy = creating one file.

## Status: MVP (Phase 0/1/2)

- [x] Project skeleton
- [x] Core engine abstractions
- [x] Data provider (yfinance + Alpaca)
- [x] Broker (Alpaca + paper)
- [x] Risk manager
- [x] PullbackEMA strategy
- [x] Backtester
- [ ] Live wiring (paper trading)
- [ ] Telegram notifications
- [ ] Streamlit dashboard
- [ ] EC2 deployment

## Quick start

```bash
# Install deps (creates .venv automatically)
uv sync

# Copy env template
cp .env.example .env
# ...then edit .env with your Alpaca keys

# Run a backtest
uv run swingbot backtest --strategy pullback_ema --start 2023-01-01 --end 2024-12-31

# Run a live scan (paper trading mode)
uv run swingbot scan --strategy pullback_ema
```

## Architecture

See `docs/architecture.md`. Key idea: every strategy implements the
`Strategy` ABC in `core/strategy_base.py`. The engine, broker, data
provider, and risk manager are all interface-based and pluggable.

```
core/         — Abstractions (Strategy, Signal, Engine, Registry)
strategies/   — Concrete strategies (one file each)
data/         — DataProvider implementations
execution/    — Broker implementations
risk/         — Risk manager
backtest/     — Backtester + metrics
persistence/  — SQLAlchemy models + repositories
notifications/— Alert channels
```

## Adding a new strategy

1. Create `strategies/my_strategy.py`
2. Subclass `core.strategy_base.Strategy`
3. Implement `universe()`, `indicators()`, `should_enter()`, `should_exit()`
4. Add config entry in `config/strategies/my_strategy.yaml`
5. Enable it in `config/config.yaml`

The registry auto-discovers it. No core changes needed.
