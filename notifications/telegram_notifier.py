"""Telegram bot notifier.

Uses the Bot API directly (no python-telegram-bot dependency — it's heavy and
mostly long-poll-oriented; we just want fire-and-forget).

Setup: see ./docs/telegram-setup.md
  1. Talk to @BotFather, /newbot → save token in .env as TELEGRAM_BOT_TOKEN
  2. Send /start to your bot from your account
  3. Visit https://api.telegram.org/bot<TOKEN>/getUpdates → grab `chat.id`
  4. Save in .env as TELEGRAM_CHAT_ID
"""

from __future__ import annotations

import logging

import httpx

from notifications.notifier_base import Notification, Notifier

log = logging.getLogger(__name__)


class TelegramNotifier(Notifier):
    name = "telegram"
    API_BASE = "https://api.telegram.org"
    TIMEOUT = 5.0  # seconds; trading must not hang on a notify

    def __init__(self, bot_token: str, chat_id: str):
        if not bot_token or not chat_id:
            raise ValueError("TelegramNotifier needs both bot_token and chat_id")
        self.bot_token = bot_token
        self.chat_id = chat_id

    def send(self, n: Notification) -> bool:
        text = self._format(n)
        url = f"{self.API_BASE}/bot{self.bot_token}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "MarkdownV2" if n.markdown else None,
            "disable_web_page_preview": True,
        }
        # Strip None
        payload = {k: v for k, v in payload.items() if v is not None}
        try:
            with httpx.Client(timeout=self.TIMEOUT) as client:
                resp = client.post(url, json=payload)
                if resp.status_code != 200:
                    log.warning("telegram send failed: %s %s", resp.status_code, resp.text[:200])
                    return False
                return True
        except Exception as e:  # network error, DNS, etc — fail-soft
            log.warning("telegram send raised: %s: %s", type(e).__name__, e)
            return False

    @staticmethod
    def _format(n: Notification) -> str:
        # MarkdownV2 has many reserved chars; we'll just bold the title and
        # let the body be plain. Caller can pre-escape if needed.
        if n.markdown:
            title = TelegramNotifier._escape_md(n.title)
            return f"*{title}*\n{n.body}"
        return f"{n.title}\n\n{n.body}"

    @staticmethod
    def _escape_md(s: str) -> str:
        # Bare-minimum escape for MarkdownV2 in title
        for ch in r"_*[]()~`>#+-=|{}.!":
            s = s.replace(ch, f"\\{ch}")
        return s


# ----------------- formatters -----------------
def fmt_signal(strategy: str, symbol: str, entry: float, stop: float, target: float, qty: int | None = None) -> Notification:
    body_lines = [
        f"Strategy: {strategy}",
        f"Symbol:   {symbol}",
        f"Entry:    ${entry:.2f}",
        f"Stop:     ${stop:.2f}  (-{(entry-stop)/entry*100:.2f}%)",
        f"Target:   ${target:.2f}  (+{(target-entry)/entry*100:.2f}%)",
    ]
    if qty is not None:
        body_lines.append(f"Qty:      {qty}")
    return Notification(kind="signal", title=f"📈 SIGNAL: {symbol}", body="\n".join(body_lines))


def fmt_order_submitted(symbol: str, qty: int, entry: float, broker_order_id: str) -> Notification:
    return Notification(
        kind="order_submitted",
        title=f"✅ ORDER: {symbol}",
        body=f"Submitted bracket order {broker_order_id}\nQty {qty} @ ~${entry:.2f}",
    )


def fmt_order_failed(symbol: str, reason: str) -> Notification:
    return Notification(
        kind="order_failed",
        title=f"❌ ORDER FAILED: {symbol}",
        body=reason,
    )


def fmt_summary(
    mode: str,
    n_signals: int,
    n_submitted: int,
    n_skipped: int,
    n_rejected: int,
    realized_pnl: float | None,
) -> Notification:
    pnl_line = f"\nRealized PnL today: ${realized_pnl:+,.2f}" if realized_pnl is not None else ""
    return Notification(
        kind="summary",
        title=f"📊 RUN SUMMARY ({mode})",
        body=(
            f"Signals: {n_signals}\n"
            f"Submitted: {n_submitted}\n"
            f"Skipped (idempotent): {n_skipped}\n"
            f"Rejected (risk): {n_rejected}"
            f"{pnl_line}"
        ),
    )


def fmt_error(where: str, exc: Exception) -> Notification:
    return Notification(
        kind="error",
        title=f"🚨 ERROR @ {where}",
        body=f"{type(exc).__name__}: {exc}",
    )
