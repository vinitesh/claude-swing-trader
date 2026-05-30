"""Project-wide logging via loguru."""

from __future__ import annotations

import sys
from pathlib import Path

from loguru import logger


def setup_logging(level: str = "INFO", log_dir: str | Path = "./logs") -> None:
    """Initialize loguru with both stdout (rich) and rotating-file sinks."""
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)

    logger.remove()  # drop default sink

    logger.add(
        sys.stdout,
        level=level,
        format=(
            "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
            "<level>{level: <8}</level> | "
            "<cyan>{name}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>"
        ),
        colorize=True,
        backtrace=False,
        diagnose=False,
    )

    logger.add(
        log_dir / "swingbot.log",
        level=level,
        rotation="10 MB",
        retention="14 days",
        compression="gz",
        enqueue=True,
        backtrace=True,
        diagnose=False,
    )


__all__ = ["logger", "setup_logging"]
