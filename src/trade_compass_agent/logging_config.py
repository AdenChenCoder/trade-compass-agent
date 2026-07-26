"""Structured logging configuration for Trade Compass Agent.

Usage:
    from trade_compass_agent.logging_config import setup_logging
    setup_logging()  # Call once at startup (cli.py / app.py)

Supports:
- Console output with color (human-readable)
- JSON structured output (for log aggregation)
- Per-module log level overrides
- Environment variable control
"""

from __future__ import annotations

import logging
import os
import re
import sys
from datetime import datetime, timezone
from typing import Any


_URL_QUERY_RE = re.compile(r"\b(?P<base>(?:https?|wss?)://[^\s?#]+)\?[^\s]+")
_SENSITIVE_PAIR_RE = re.compile(
    r"(?i)\b(?P<key>"
    r"access[_-]?key|api[_-]?key|app[_-]?secret|client[_-]?secret|"
    r"password|signature|ticket|token"
    r")=(?P<value>[^\s&,;]+)"
)
_BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")


def redact_log_text(value: str) -> str:
    """Remove common credential shapes before a log record is emitted."""
    value = _URL_QUERY_RE.sub(r"\g<base>?<redacted>", value)
    value = _SENSITIVE_PAIR_RE.sub(r"\g<key>=<redacted>", value)
    return _BEARER_RE.sub("Bearer <redacted>", value)


def _redact_log_value(value: Any) -> Any:
    if isinstance(value, str):
        return redact_log_text(value)
    if isinstance(value, dict):
        return {key: _redact_log_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_log_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_log_value(item) for item in value)
    return value


class SensitiveLogFilter(logging.Filter):
    """Redact credentials from application and third-party log records."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact_log_text(record.getMessage())
        record.args = ()
        if hasattr(record, "extra_data"):
            record.extra_data = _redact_log_value(record.extra_data)
        return True


class StructuredFormatter(logging.Formatter):
    """JSON-line formatter for structured log output."""

    def format(self, record: logging.LogRecord) -> str:
        import json

        entry: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": redact_log_text(record.getMessage()),
        }
        if record.exc_info and record.exc_info[1]:
            entry["error"] = redact_log_text(str(record.exc_info[1]))
            entry["error_type"] = type(record.exc_info[1]).__name__
        if hasattr(record, "extra_data"):
            entry["data"] = _redact_log_value(record.extra_data)
        return json.dumps(entry, ensure_ascii=False)


class ConsoleFormatter(logging.Formatter):
    """Human-readable colored console output."""

    COLORS = {
        "DEBUG": "\033[36m",  # cyan
        "INFO": "\033[32m",  # green
        "WARNING": "\033[33m",  # yellow
        "ERROR": "\033[31m",  # red
        "CRITICAL": "\033[35m",  # magenta
    }
    RESET = "\033[0m"

    def format(self, record: logging.LogRecord) -> str:
        color = self.COLORS.get(record.levelname, "")
        reset = self.RESET if color else ""
        timestamp = datetime.fromtimestamp(record.created).strftime("%H:%M:%S")
        name_short = record.name.replace("trade_compass_agent.", "")
        message = redact_log_text(record.getMessage())
        return f"{color}{timestamp} [{record.levelname[0]}] {name_short}: {message}{reset}"


def setup_logging(
    level: str | None = None,
    structured: bool | None = None,
) -> None:
    """Configure logging for the application.

    Args:
        level: Log level (DEBUG/INFO/WARNING/ERROR). Default from LOG_LEVEL env or INFO.
        structured: If True, output JSON lines. Default from LOG_FORMAT=json env.
    """
    log_level = (level or os.getenv("LOG_LEVEL", "INFO")).upper()
    use_structured = (
        structured if structured is not None else (os.getenv("LOG_FORMAT", "").lower() == "json")
    )

    root = logging.getLogger()
    root.setLevel(getattr(logging, log_level, logging.INFO))

    # Remove existing handlers
    for handler in root.handlers[:]:
        root.removeHandler(handler)

    handler = logging.StreamHandler(sys.stderr)
    handler.addFilter(SensitiveLogFilter())
    if use_structured:
        handler.setFormatter(StructuredFormatter())
    else:
        handler.setFormatter(ConsoleFormatter())
    root.addHandler(handler)

    # Quiet noisy third-party loggers
    for noisy in ("urllib3", "httpx", "httpcore", "openai", "akshare", "Lark", "lark_oapi"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    # lark_oapi installs its own stdout handler and may log signed WebSocket
    # URLs. A logger-level filter also covers handlers created after startup.
    lark_logger = logging.getLogger("Lark")
    for existing_filter in list(lark_logger.filters):
        if isinstance(existing_filter, SensitiveLogFilter):
            lark_logger.removeFilter(existing_filter)
    lark_logger.addFilter(SensitiveLogFilter())

    # Per-module overrides from environment (e.g. LOG_LEVEL_SCREENING=DEBUG)
    for key, value in os.environ.items():
        if key.startswith("LOG_LEVEL_") and key != "LOG_LEVEL":
            module_suffix = key[len("LOG_LEVEL_") :].lower().replace("_", ".")
            module_name = f"trade_compass_agent.{module_suffix}"
            logging.getLogger(module_name).setLevel(getattr(logging, value.upper(), logging.INFO))
