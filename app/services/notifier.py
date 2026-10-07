"""Optional Telegram notifications (fills, rejections, errors, kill switch)."""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Protocol

import requests

from app.config import Settings

log = logging.getLogger(__name__)


class Notifier(Protocol):
    def send(self, text: str) -> None: ...
    def close(self) -> None: ...


class NullNotifier:
    def send(self, text: str) -> None:
        pass

    def close(self) -> None:
        pass


class TelegramNotifier:
    """Fire-and-forget: messages go out on a background thread so a slow
    Telegram API never delays order handling."""

    def __init__(self, token: str, chat_id: str, prefix: str, client: requests.Session | None = None):
        self._url = f"https://api.telegram.org/bot{token}/sendMessage"
        self._chat_id = chat_id
        self._prefix = prefix
        self._client = client or requests.Session()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="telegram")

    def send(self, text: str) -> None:
        self._executor.submit(self._deliver, f"[{self._prefix}] {text}"[:4000])

    def _deliver(self, text: str) -> None:
        try:
            response = self._client.post(
                self._url, json={"chat_id": self._chat_id, "text": text, "disable_web_page_preview": True}, timeout=5
            )
            if response.status_code != 200:
                log.warning("telegram send failed", extra={"status_code": response.status_code})
        except Exception as exc:
            # Log only the type: request errors include the URL, which contains the bot token.
            log.warning("telegram send failed", extra={"error_type": type(exc).__name__})

    def close(self) -> None:
        self._executor.shutdown(wait=True)
        self._client.close()


def build_notifier(settings: Settings) -> Notifier:
    if settings.telegram_bot_token and settings.telegram_chat_id:
        return TelegramNotifier(settings.telegram_bot_token.get_secret_value(), settings.telegram_chat_id, settings.mode_label)
    return NullNotifier()
