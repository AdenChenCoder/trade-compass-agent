from __future__ import annotations

from trade_compass_agent.runtime.activity_events import (
    build_tool_start_payload,
    redact_args,
    summarize_tool_label,
    tool_kind,
)


def test_summarize_tool_label_get_bars() -> None:
    label = summarize_tool_label("get_bars", {"symbol": "600519", "timeframe": "1d"})
    assert label == "get_bars(600519, 1d)"


def test_summarize_tool_label_mcp() -> None:
    label = summarize_tool_label("mcp_github_search", {})
    assert label == "mcp:github/search"


def test_redact_secret_args() -> None:
    redacted = redact_args({"api_key": "sk-secret", "symbol": "600519"})
    assert redacted["api_key"] == "***"
    assert redacted["symbol"] == "600519"


def test_build_tool_start_payload_includes_kind_and_label() -> None:
    payload = build_tool_start_payload("load_skill", '{"name": "compliance"}')
    assert payload["kind"] == "skill"
    assert payload["label"] == "load_skill(compliance)"
    assert payload["arguments"]["name"] == "compliance"


def test_tool_kind_dispatch_specialists() -> None:
    assert tool_kind("dispatch_specialists") == "specialist"
