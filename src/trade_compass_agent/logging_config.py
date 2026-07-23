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
import sys
from datetime import datetime, timezone
from typing import Any


class StructuredFormatter(logging.Formatter):
    """JSON-line formatter for structured log output."""

    def format(self, record: logging.LogRecord) -> str:
        import json

        entry: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info and record.exc_info[1]:
            entry["error"] = str(record.exc_info[1])
            entry["error_type"] = type(record.exc_info[1]).__name__
        if hasattr(record, "extra_data"):
            entry["data"] = record.extra_data
        return json.dumps(entry, ensure_ascii=False)


class ConsoleFormatter(logging.Formatter):
    """Human-readable colored console output."""

    COLORS = {
        "DEBUG": "\033[36m",     # cyan
        "INFO": "\033[32m",      # green
        "WARNING": "\033[33m",   # yellow
        "ERROR": "\033[31m",     # red
        "CRITICAL": "\033[35m",  # magenta
    }
    RESET = "\033[0m"

    def format(self, record: logging.LogRecord) -> str:
        color = self.COLORS.get(record.levelname, "")
        reset = self.RESET if color else ""
        timestamp = datetime.fromtimestamp(record.created).strftime("%H:%M:%S")
        name_short = record.name.replace("trade_compass_agent.", "")
        return f"{color}{timestamp} [{record.levelname[0]}] {name_short}: {record.getMessage()}{reset}"


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
    use_structured = structured if structured is not None else (
        os.getenv("LOG_FORMAT", "").lower() == "json"
    )

    root = logging.getLogger()
    root.setLevel(getattr(logging, log_level, logging.INFO))

    # Remove existing handlers
    for handler in root.handlers[:]:
        root.removeHandler(handler)

    handler = logging.StreamHandler(sys.stderr)
    if use_structured:
        handler.setFormatter(StructuredFormatter())
    else:
        handler.setFormatter(ConsoleFormatter())
    root.addHandler(handler)

    # Quiet noisy third-party loggers
    for noisy in ("urllib3", "httpx", "httpcore", "openai", "akshare"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    # Per-module overrides from environment (e.g. LOG_LEVEL_SCREENING=DEBUG)
    for key, value in os.environ.items():
        if key.startswith("LOG_LEVEL_") and key != "LOG_LEVEL":
            module_suffix = key[len("LOG_LEVEL_"):].lower().replace("_", ".")
            module_name = f"trade_compass_agent.{module_suffix}"
            logging.getLogger(module_name).setLevel(getattr(logging, value.upper(), logging.INFO))
