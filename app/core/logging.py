"""Structured logging.

`LOG_FORMAT=json` emits one JSON object per line, which CloudWatch Logs Insights
can query directly on fields like `transaction_id`, `event_type` and `status`.
A redaction filter guarantees that configured secret values never reach a log
sink even if some code accidentally passes them through.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from contextvars import ContextVar
from typing import Any

from app.core.config import Settings, get_settings

REDACTED = "***REDACTED***"

_SENSITIVE_KEYS = {
    "password",
    "secret",
    "signature",
    "api_key",
    "apikey",
    "authorization",
    "key_secret",
    "razorpay_key_secret",
    "ap2_mandate_secret",
    "aws_secret_access_key",
    "webhook_secret",
    "token",
}

_transaction_id: ContextVar[str | None] = ContextVar("transaction_id", default=None)

_STANDARD_ATTRS = set(
    logging.LogRecord("", 0, "", 0, "", None, None).__dict__.keys()
) | {"asctime", "message", "taskName"}


def set_transaction_id(transaction_id: str | None) -> None:
    _transaction_id.set(transaction_id)


def get_transaction_id() -> str | None:
    return _transaction_id.get()


def redact_mapping(data: Any, depth: int = 0) -> Any:
    """Recursively redact obviously sensitive keys in a payload."""
    if depth > 6:
        return "..."
    if isinstance(data, dict):
        out: dict[str, Any] = {}
        for key, value in data.items():
            if any(token in str(key).lower() for token in _SENSITIVE_KEYS):
                out[str(key)] = REDACTED
            else:
                out[str(key)] = redact_mapping(value, depth + 1)
        return out
    if isinstance(data, (list, tuple)):
        return [redact_mapping(item, depth + 1) for item in data]
    return data


class SecretRedactionFilter(logging.Filter):
    """Replace known secret values anywhere in the rendered log line."""

    def __init__(self, secrets: list[str]) -> None:
        super().__init__()
        self._secrets = [s for s in secrets if s]

    def filter(self, record: logging.LogRecord) -> bool:
        if not self._secrets:
            return True
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - defensive
            return True
        for secret in self._secrets:
            if secret in message:
                message = message.replace(secret, REDACTED)
                record.msg = message
                record.args = ()
        for key, value in list(record.__dict__.items()):
            if isinstance(value, str):
                for secret in self._secrets:
                    if secret in value:
                        record.__dict__[key] = value.replace(secret, REDACTED)
        return True


class JsonFormatter(logging.Formatter):
    """CloudWatch-friendly single-line JSON formatter."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": time.strftime(
                "%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)
            )
            + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        transaction_id = getattr(record, "transaction_id", None) or get_transaction_id()
        if transaction_id:
            payload["transaction_id"] = transaction_id
        for key, value in record.__dict__.items():
            if key in _STANDARD_ATTRS or key.startswith("_"):
                continue
            payload[key] = value
        if record.exc_info:
            payload["error_type"] = record.exc_info[0].__name__ if record.exc_info[0] else None
            payload["stack"] = self.formatException(record.exc_info)
        return json.dumps(redact_mapping(payload), default=str, ensure_ascii=False)


class ConsoleFormatter(logging.Formatter):
    """Readable formatter for local development."""

    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        extras = {
            key: value
            for key, value in record.__dict__.items()
            if key not in _STANDARD_ATTRS and not key.startswith("_")
        }
        transaction_id = extras.pop("transaction_id", None) or get_transaction_id()
        if transaction_id:
            base = f"{base} [tx={transaction_id}]"
        if extras:
            base = f"{base} {json.dumps(redact_mapping(extras), default=str)}"
        return base


_configured = False


def configure_logging(settings: Settings | None = None, *, force: bool = False) -> None:
    """Install handlers on the root logger (idempotent)."""
    global _configured
    if _configured and not force:
        return
    settings = settings or get_settings()

    handler = logging.StreamHandler(stream=sys.stdout)
    if settings.log_format == "json" or settings.is_production:
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(
            ConsoleFormatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
        )
    handler.addFilter(SecretRedactionFilter(settings.secret_values()))

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(settings.log_level)

    logging.getLogger("uvicorn.access").setLevel("WARNING")
    logging.getLogger("botocore").setLevel("WARNING")
    logging.getLogger("boto3").setLevel("WARNING")
    _configured = True


def get_logger(name: str) -> logging.Logger:
    configure_logging()
    return logging.getLogger(name)


def log_event(
    logger: logging.Logger,
    message: str,
    *,
    level: int = logging.INFO,
    **fields: Any,
) -> None:
    """Emit a structured event with redacted extra fields."""
    safe = {
        (f"field_{key}" if key in _STANDARD_ATTRS else key): value
        for key, value in fields.items()
    }
    logger.log(level, message, extra=redact_mapping(safe))
