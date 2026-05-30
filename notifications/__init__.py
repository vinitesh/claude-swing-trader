"""Notifier factory.

build_notifier(settings, config) → Notifier (TelegramNotifier or NullNotifier).
Decoupled from CLI so the engine can use it without depending on click/rich.
"""

from __future__ import annotations

import logging
from typing import Any

from notifications.notifier_base import NullNotifier, Notifier
from notifications.telegram_notifier import TelegramNotifier

log = logging.getLogger(__name__)


def build_notifier(settings: Any, config: dict) -> Notifier:
    """Pick a notifier based on settings + YAML.

    Precedence:
      - YAML notifications.telegram.enabled = false → NullNotifier
      - Telegram creds missing → NullNotifier (warn)
      - Else TelegramNotifier
    """
    notify_cfg = (config.get("notifications") or {}).get("telegram") or {}
    if not notify_cfg.get("enabled", False):
        return NullNotifier()
    token = getattr(settings, "telegram_bot_token", "") or ""
    chat = getattr(settings, "telegram_chat_id", "") or ""
    if not token or not chat:
        log.warning("Telegram enabled in YAML but bot_token/chat_id missing — using NullNotifier")
        return NullNotifier()
    try:
        return TelegramNotifier(bot_token=token, chat_id=chat)
    except Exception as e:
        log.warning("Failed to construct TelegramNotifier: %s — using NullNotifier", e)
        return NullNotifier()
