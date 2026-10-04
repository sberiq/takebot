"""Redact credential-shaped values before they reach application logs."""

from __future__ import annotations

import logging
import re
import traceback


_SECRET_PATTERNS = (
    re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{25,}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b"),
)


def _redact(value: str) -> str:
    for pattern in _SECRET_PATTERNS:
        value = pattern.sub("[REDACTED_SECRET]", value)
    return value


class SecretRedactionFilter(logging.Filter):
    """Redact Telegram bot tokens and Google API keys in messages and traces."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = _redact(record.getMessage())
        record.args = ()
        if record.exc_info:
            record.exc_text = _redact("".join(traceback.format_exception(*record.exc_info)))
            record.exc_info = None
        elif record.exc_text:
            record.exc_text = _redact(record.exc_text)
        return True


def install_secret_redaction() -> None:
    root = logging.getLogger()
    for handler in root.handlers:
        if not any(isinstance(item, SecretRedactionFilter) for item in handler.filters):
            handler.addFilter(SecretRedactionFilter())
