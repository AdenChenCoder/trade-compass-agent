"""Tests for budget.py — token estimation and threshold decisions."""

from __future__ import annotations

import pytest

from trade_compass_agent.llm.providers import ChatMessage
from trade_compass_agent.runtime.compression.budget import (
    TokenBudget,
    estimate_tokens,
    estimate_message_tokens,
    estimate_messages_tokens,
    estimate_request_tokens,
    resolve_context_budget,
)


# ---- Config stub ----
class _MockCompressionConfig:
    enabled = True
    trim_threshold_pct = 0.60
    summary_threshold_pct = 0.80
    emergency_threshold_pct = 0.95
    protect_recent_count = 20
    protect_recent_tokens = 16000
    context_budget = 0  # auto-detect


class _MockLLMConfig:
    model = "deepseek-chat"


class _MockConfig:
    llm = _MockLLMConfig()
    context_compression = _MockCompressionConfig()


# ---- Token estimation ----

@pytest.mark.parametrize(
    "text,expected_min,expected_max",
    [
        ("Hello world", 2, 5),
        ("你好世界", 2, 4),
        ("600519 短线怎么看", 4, 10),
        ("", 0, 0),
    ],
)
def test_estimate_tokens_range(text, expected_min, expected_max):
    result = estimate_tokens(text)
    assert expected_min <= result <= expected_max


def test_estimate_message_tokens_basic():
    msg = ChatMessage(role="user", content="Hello world")
    tokens = estimate_message_tokens(msg)
    assert tokens >= 4  # content + role overhead


def test_estimate_message_tokens_with_tool_calls():
    msg = ChatMessage(
        role="assistant",
        content="Let me check",
        tool_calls=[{
            "id": "call_1",
            "function": {"name": "get_bars", "arguments": '{"symbol":"600519"}'},
        }],
    )
    tokens = estimate_message_tokens(msg)
    assert tokens > estimate_tokens("Let me check")


def test_estimate_messages_tokens():
    msgs = [
        ChatMessage(role="system", content="You are a trading assistant."),
        ChatMessage(role="user", content="600519 怎么看"),
        ChatMessage(role="assistant", content="我来查一下行情"),
    ]
    total = estimate_messages_tokens(msgs)
    assert total > 20


def test_estimate_request_tokens_with_tools():
    msgs = [ChatMessage(role="user", content="test")]
    tools = [{"type": "function", "function": {"name": "get_bars", "description": "..."}}]
    total = estimate_request_tokens(msgs, tools_schemas=tools, system_prompt_tokens=100)
    assert total > 100


# ---- Context budget resolution ----

def test_resolve_context_budget_auto():
    config = _MockConfig()
    budget = resolve_context_budget(config)
    assert budget == 128_000  # deepseek-chat


class _ExplicitBudgetConfig(_MockConfig):
    class _Comp(_MockCompressionConfig):
        context_budget = 64000
    context_compression = _Comp()


def test_resolve_context_budget_explicit():
    budget = resolve_context_budget(_ExplicitBudgetConfig())
    assert budget == 64000


# ---- Threshold checks ----

def test_should_trim_below_threshold():
    budget = TokenBudget(_MockConfig())
    assert budget.should_trim(10_000) is False


def test_should_trim_above_threshold():
    budget = TokenBudget(_MockConfig())
    # 60% of 128k = 76800
    assert budget.should_trim(80_000) is True


def test_should_summarize():
    budget = TokenBudget(_MockConfig())
    # 80% of 128k = 102400
    assert budget.should_summarize(50_000) is False
    assert budget.should_summarize(110_000) is True


def test_is_emergency():
    budget = TokenBudget(_MockConfig())
    # 95% of 128k = 121600
    assert budget.is_emergency(50_000) is False
    assert budget.is_emergency(125_000) is True


def test_usage_pct():
    budget = TokenBudget(_MockConfig())
    assert budget.usage_pct(64_000) == 0.5
    assert budget.usage_pct(0) == 0.0


def test_disabled():
    cfg = _MockConfig()
    cfg.context_compression.enabled = False
    budget = TokenBudget(cfg)
    assert budget.enabled is False
    assert budget.should_trim(999_999) is False
    assert budget.should_summarize(999_999) is False
    assert budget.is_emergency(999_999) is False


# ---- Overflow error detection ----

@pytest.mark.parametrize(
    "error_text",
    [
        "This request exceeds the context window of this model",
        "Maximum context length exceeded",
        "too many tokens in the prompt",
        "Token limit exceeded for request",
        "The prompt is too long, please reduce",
        "Input is too long for this model",
        "context length exceeded — 200k limit",
    ],
)
def test_is_context_overflow_error_true(error_text):
    assert TokenBudget.is_context_overflow_error(error_text) is True


@pytest.mark.parametrize(
    "error_text",
    [
        "API rate limit exceeded",
        "invalid API key",
        "model not found",
        "",
    ],
)
def test_is_context_overflow_error_false(error_text):
    assert TokenBudget.is_context_overflow_error(error_text) is False
