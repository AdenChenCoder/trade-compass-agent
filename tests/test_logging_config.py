from __future__ import annotations

import json
import logging

from trade_compass_agent.logging_config import (
    SensitiveLogFilter,
    StructuredFormatter,
    redact_log_text,
    setup_logging,
)


def test_redact_log_text_removes_url_queries_and_credentials() -> None:
    message = (
        "connected wss://example.test/connect?access_key=secret&ticket=once "
        "app_secret=hidden Authorization: Bearer header.payload.signature"
    )

    redacted = redact_log_text(message)

    assert "access_key=secret" not in redacted
    assert "ticket=once" not in redacted
    assert "app_secret=hidden" not in redacted
    assert "header.payload.signature" not in redacted
    assert "wss://example.test/connect?<redacted>" in redacted
    assert "app_secret=<redacted>" in redacted
    assert "Bearer <redacted>" in redacted


def test_sensitive_log_filter_redacts_percent_style_arguments() -> None:
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="request token=%s",
        args=("top-secret",),
        exc_info=None,
    )

    assert SensitiveLogFilter().filter(record)
    assert record.getMessage() == "request token=<redacted>"
    assert record.args == ()


def test_structured_formatter_redacts_nested_extra_data() -> None:
    record = logging.LogRecord(
        name="test",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg="failed",
        args=(),
        exc_info=None,
    )
    record.extra_data = {
        "endpoint": "https://example.test/hook?signature=secret",
        "headers": ["Bearer bearer-secret"],
    }

    payload = json.loads(StructuredFormatter().format(record))

    assert "secret" not in json.dumps(payload)
    assert payload["data"]["endpoint"] == "https://example.test/hook?<redacted>"
    assert payload["data"]["headers"] == ["Bearer <redacted>"]


def test_setup_logging_quiets_and_filters_lark_logger() -> None:
    setup_logging(level="INFO")

    lark_logger = logging.getLogger("Lark")
    assert lark_logger.level == logging.WARNING
    assert any(isinstance(item, SensitiveLogFilter) for item in lark_logger.filters)
