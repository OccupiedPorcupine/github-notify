"""Structured JSON logging with secret redaction (§10: never log the token)."""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone

_SECRETS: list[str] = []


def register_secret(value: str | None) -> None:
    """Register a value to be scrubbed from every log record.

    Belt and braces. We never deliberately log a token, but a stray exception
    repr carrying a header dict would otherwise leak one.
    """
    if value and len(value) >= 8:
        _SECRETS.append(value)


def _scrub(text: str) -> str:
    for secret in _SECRETS:
        if secret in text:
            text = text.replace(secret, "<redacted>")
    return text


_STD_ATTRS = frozenset(
    logging.LogRecord("", 0, "", 0, "", None, None).__dict__.keys()
) | {"message", "asctime", "taskName"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _STD_ATTRS:
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return _scrub(json.dumps(payload, ensure_ascii=False, default=str))


def setup_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(os.environ.get("GHN_LOG_LEVEL", "INFO").upper())
    # httpx logs every request at INFO, including the full URL. Quiet it.
    logging.getLogger("httpx").setLevel(logging.WARNING)
