"""Notifier tests — focus on factory behavior + fail-soft + formatters.

We do NOT hit the real Telegram API; httpx is monkey-patched in the one
delivery test so the suite stays offline.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from notifications import build_notifier
from notifications.notifier_base import NullNotifier, Notification
from notifications.telegram_notifier import (
    TelegramNotifier,
    fmt_signal,
    fmt_summary,
)


class _Settings:
    telegram_bot_token = ""
    telegram_chat_id = ""


def test_factory_returns_null_when_disabled():
    s = _Settings()
    cfg = {"notifications": {"telegram": {"enabled": False}}}
    n = build_notifier(s, cfg)
    assert isinstance(n, NullNotifier)


def test_factory_returns_null_when_creds_missing(caplog):
    s = _Settings()
    cfg = {"notifications": {"telegram": {"enabled": True}}}
    n = build_notifier(s, cfg)
    assert isinstance(n, NullNotifier)


def test_factory_builds_telegram():
    s = _Settings()
    s.telegram_bot_token = "FAKE:TOKEN"
    s.telegram_chat_id = "12345"
    cfg = {"notifications": {"telegram": {"enabled": True}}}
    n = build_notifier(s, cfg)
    assert isinstance(n, TelegramNotifier)


def test_telegram_send_failsoft_on_network_error():
    n = TelegramNotifier(bot_token="X", chat_id="Y")
    with patch("httpx.Client.post", side_effect=ConnectionError("boom")):
        ok = n.send(Notification(kind="signal", title="hi", body="world"))
    assert ok is False  # failed but did not raise


def test_telegram_send_failsoft_on_non_200():
    n = TelegramNotifier(bot_token="X", chat_id="Y")
    class _Resp:
        status_code = 401
        text = "unauthorized"
    with patch("httpx.Client.post", return_value=_Resp()):
        ok = n.send(Notification(kind="signal", title="hi", body="world"))
    assert ok is False


def test_telegram_send_succeeds():
    n = TelegramNotifier(bot_token="X", chat_id="Y")
    class _Resp:
        status_code = 200
        text = "ok"
    with patch("httpx.Client.post", return_value=_Resp()):
        ok = n.send(Notification(kind="signal", title="hi", body="world"))
    assert ok is True


def test_signal_formatter_includes_key_fields():
    notif = fmt_signal("pullback_ema", "AAPL", 100.0, 98.0, 106.0, qty=10)
    assert "AAPL" in notif.title
    assert "Strategy: pullback_ema" in notif.body
    assert "$100.00" in notif.body
    assert "Stop:" in notif.body and "$98.00" in notif.body
    assert "Target:" in notif.body and "$106.00" in notif.body
    assert "Qty:" in notif.body


def test_summary_formatter_includes_pnl():
    notif = fmt_summary(mode="paper", n_signals=3, n_submitted=2, n_skipped=1, n_rejected=0, realized_pnl=125.55)
    assert "Signals: 3" in notif.body
    assert "Submitted: 2" in notif.body
    assert "$+125.55" in notif.body
