"""Structured JSON logging with secret redaction."""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Iterable
from datetime import UTC, datetime

# Standard LogRecord attributes, plus uvicorn's ANSI-coloured duplicate of the message.
_STANDARD_ATTRS = set(vars(logging.makeLogRecord({}))) | {"message", "asctime", "taskName", "color_message"}
REDACTED = "***REDACTED***"

# Loggers that would print request URLs or bodies. The Telegram API URL embeds
# the bot token, so httpx must never log at INFO.
_NOISY_LOGGERS = ("httpx", "httpcore", "urllib3", "pybit", "websocket")


class JsonFormatter(logging.Formatter):
    """One JSON object per line. Fields passed via ``extra=`` become top-level keys.

    As a last line of defence, any configured secret value appearing anywhere in
    the rendered line is replaced with ``***REDACTED***``.
    """

    def __init__(self, secrets: Iterable[str] = ()):
        super().__init__()
        self._secrets = sorted({s for s in secrets if s and len(s) >= 6}, key=len, reverse=True)

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _STANDARD_ATTRS and not key.startswith("_"):
                entry[key] = value
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        line = json.dumps(entry, default=str, ensure_ascii=False)
        for secret in self._secrets:
            line = line.replace(secret, REDACTED)
        return line


def setup_logging(level: str, secrets: Iterable[str]) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter(secrets))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    # Route uvicorn's own loggers through the JSON handler.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.handlers[:] = []
        logger.propagate = True
    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
