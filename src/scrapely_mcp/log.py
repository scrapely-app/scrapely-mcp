"""Structured logging to stderr with credential redaction.

Every record is printed as `LEVEL message {json fields}` on stderr. stdout is
reserved for the stdio protocol channel.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sys
from typing import Any

LOGGER_NAME = "scrapely_mcp"
REDACTED = "[REDACTED]"

_SENSITIVE_KEYS = {
    "authorization",
    "token",
    "scrapelytoken",
    "apifytoken",
    "skyfire-pay-id",
    "payment-signature",
    "x402/payment",
    "x-payment",
    "password",
    "secret",
    "apikey",
    "api_key",
}

_BEARER_RE = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=\-]+")
_TOKEN_QUERY_RE = re.compile(r"(?i)([?&](?:token|skyfire-pay-id)=)[^&\s\"']+")
_ERROR_WORD_RE = re.compile(r"(?i)\berror\b")

# Values registered at runtime (current tokens / payment ids) are scrubbed from any text.
_secret_values: set[str] = set()


def register_secret(value: str | None) -> None:
    if value and len(value) >= 6:
        _secret_values.add(value)


def token_fingerprint(token: str | None) -> str:
    return hashlib.sha256((token or "").encode()).hexdigest()


def scrub_text(text: str) -> str:
    text = _BEARER_RE.sub(lambda m: m.group(1) + REDACTED, text)
    text = _TOKEN_QUERY_RE.sub(lambda m: m.group(1) + REDACTED, text)
    for secret in list(_secret_values):
        if secret in text:
            text = text.replace(secret, REDACTED)
    return text


def redact(value: Any, _depth: int = 0) -> Any:
    if _depth > 8:
        return "..."
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            if str(k).lower() in _SENSITIVE_KEYS:
                out[k] = REDACTED
            else:
                out[k] = redact(v, _depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [redact(v, _depth + 1) for v in value]
    if isinstance(value, str):
        return scrub_text(value)
    return value


class _Formatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        message = record.getMessage()
        fields = getattr(record, "fields", None)
        soft = getattr(record, "soft", False)
        if soft:
            message = "[SOFT] " + _ERROR_WORD_RE.sub("failure", message)
        line = f"{record.levelname} {scrub_text(message)}"
        if fields:
            try:
                line += " " + json.dumps(redact(fields), default=str, ensure_ascii=False)
            except (TypeError, ValueError):
                pass
        if record.exc_info:
            line += "\n" + scrub_text(self.formatException(record.exc_info))
        return line


_configured = False


def setup_logging(level: str | None = None) -> None:
    global _configured
    logger = logging.getLogger(LOGGER_NAME)
    lvl = (level or os.environ.get("LOG_LEVEL") or "info").upper()
    if lvl == "WARN":
        lvl = "WARNING"
    logger.setLevel(getattr(logging, lvl, logging.INFO))
    if not _configured:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(_Formatter())
        logger.addHandler(handler)
        logger.propagate = True
        _configured = True


class Log:
    """Thin facade adding structured fields and the soft-failure level."""

    def __init__(self, name: str = LOGGER_NAME) -> None:
        self._logger = logging.getLogger(name)

    def _emit(self, level: int, msg: str, fields: dict | None, soft: bool = False, exc: bool = False) -> None:
        if soft:
            msg = _ERROR_WORD_RE.sub("failure", msg)
        self._logger.log(
            level,
            scrub_text(msg),
            extra={"fields": redact(fields) if fields else None, "soft": soft},
            exc_info=exc,
        )

    def debug(self, msg: str, **fields: Any) -> None:
        self._emit(logging.DEBUG, msg, fields)

    def info(self, msg: str, **fields: Any) -> None:
        self._emit(logging.INFO, msg, fields)

    def warning(self, msg: str, **fields: Any) -> None:
        self._emit(logging.WARNING, msg, fields)

    def error(self, msg: str, exc: bool = False, **fields: Any) -> None:
        self._emit(logging.ERROR, msg, fields, exc=exc)

    def soft_fail(self, msg: str, **fields: Any) -> None:
        self._emit(logging.WARNING, msg, fields, soft=True)

    def classified(self, msg: str, err: BaseException, **fields: Any) -> None:
        """Log an API/remote failure as a soft failure or error per SPEC 13.2."""
        if is_soft_error(err):
            self.soft_fail(f"{msg}: {err}", **fields)
        else:
            self.error(f"{msg}: {err}", exc=True, **fields)


_TRANSIENT = (
    "connection reset",
    "timed out",
    "timeout",
    "bad gateway",
    "gateway timeout",
    "service unavailable",
    "socket hang up",
    "econnreset",
)


def is_soft_error(err: BaseException) -> bool:
    from .errors import ApiError, ToolError  # local import to avoid cycles

    text = str(err).lower()
    if "cannot-start-actor-runs" in text or "oversized" in text or "parse" in text and "remote" in text:
        return True
    if any(t in text for t in _TRANSIENT):
        return True
    if isinstance(err, ApiError):
        if err.type in ("cannot-start-actor-runs", "concurrency-limit-exceeded"):
            return True
        return err.status is not None and err.status < 500
    if isinstance(err, ToolError):
        return err.user
    code = getattr(err, "code", None)
    if isinstance(code, int) and code in (-32700, -32600, -32601, -32602):
        return True
    return False


log = Log()
