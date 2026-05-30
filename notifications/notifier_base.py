"""Abstract notifier interface.

Notifications are SIDE-EFFECTS, not source-of-truth events. They must
fail-soft — a Telegram outage cannot block live trading. All implementations:
  - return True on delivered, False on failure
  - never raise out of `send()` (catch + log + return False)
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class Notification:
    kind: str          # "signal" | "order_submitted" | "order_failed" | "summary" | "error"
    title: str
    body: str
    # markdown is what tg supports
    markdown: bool = True


class Notifier(ABC):
    name: str = "base"

    @abstractmethod
    def send(self, n: Notification) -> bool:
        """Send the notification. Return True if delivered, False on any failure."""
        ...


class NullNotifier(Notifier):
    """Drop-on-floor notifier for dry-runs / disabled config."""

    name = "null"

    def send(self, n: Notification) -> bool:  # noqa: D401
        return True
