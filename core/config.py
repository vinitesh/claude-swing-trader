"""Config loader: env vars + YAML, validated by pydantic.

Reads:
  1. .env (via python-dotenv) → environment variables
  2. config/config.yaml        → static defaults
  3. config/strategies/*.yaml  → per-strategy params

Settings precedence: env > YAML.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = PROJECT_ROOT / "config"


# ---------------- Env-driven settings ----------------
class Settings(BaseSettings):
    """Settings sourced from environment variables / .env file."""

    model_config = SettingsConfigDict(
        env_file=str(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # Alpaca
    alpaca_api_key: str = ""
    alpaca_secret_key: str = ""
    alpaca_base_url: str = "https://paper-api.alpaca.markets"
    alpaca_data_feed: str = "iex"

    # Mode
    trading_mode: str = "paper"     # paper | live | backtest

    # Database
    database_url: str = "sqlite:///./trading.db"

    # Logging
    log_level: str = "INFO"
    log_dir: str = "./logs"

    # Notifications
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # Risk defaults
    default_risk_pct: float = 0.01
    max_open_positions: int = 8
    daily_loss_limit_pct: float = 0.03


# ---------------- YAML config ----------------
def load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Missing YAML config: {path}")
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_global_config() -> dict[str, Any]:
    """Load config/config.yaml."""
    return load_yaml(CONFIG_DIR / "config.yaml")


def load_strategy_config(filename: str) -> dict[str, Any]:
    """Load config/strategies/<filename>."""
    return load_yaml(CONFIG_DIR / "strategies" / filename)


# ---------------- Convenience ----------------
def init() -> tuple[Settings, dict[str, Any]]:
    """Load everything: returns (env settings, global YAML config)."""
    load_dotenv(PROJECT_ROOT / ".env", override=False)
    settings = Settings()
    cfg = load_global_config()
    return settings, cfg
