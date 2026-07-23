from __future__ import annotations

from datetime import date

from trade_compass_agent.config import AppConfig
from trade_compass_agent.llm.providers import ChatMessage
from trade_compass_agent.ops.agent_session import ScheduledAgentSession, wrap_scheduler_prompt
from trade_compass_agent.runtime.context import ContextBuilder, _sanitize_tool_calls


def test_scheduled_agent_session_uses_step_scoped_id() -> None:
    session = ScheduledAgentSession(
        AppConfig(),
        job_id="premarket",
        run_date=date(2026, 6, 17),
        step_id="overnight_news",
    )
    assert session.session_id == "scheduler-premarket-overnight_news-2026-06-17"


def test_wrap_scheduler_prompt_adds_unattended_output_rules() -> None:
    wrapped = wrap_scheduler_prompt("请生成盘前建议")
    assert "无人值守" in wrapped
    assert "禁止向用户提问" in wrapped
    assert wrapped.endswith("请生成盘前建议")


def test_sanitize_tool_calls_handles_interleaved_parallel_assistants() -> None:
    history = [
        ChatMessage(
            role="assistant",
            content="global",
            tool_calls=[{"id": "call_a", "type": "function", "function": {"name": "web_search", "arguments": "{}"}}],
        ),
        ChatMessage(
            role="assistant",
            content="news",
            tool_calls=[{"id": "call_b", "type": "function", "function": {"name": "batch_search_news", "arguments": "{}"}}],
        ),
        ChatMessage(role="tool", content='{"ok": true}', tool_call_id="call_a", name="web_search"),
        ChatMessage(role="tool", content='{"ok": true}', tool_call_id="call_b", name="batch_search_news"),
    ]

    builder = ContextBuilder(memory_dir=AppConfig().memory_dir, skills=[])
    messages = builder.build_messages(history, "next turn")

    roles_and_ids = [
        (m.role, m.tool_call_id, bool(m.tool_calls))
        for m in messages
        if m.role != "system"
    ]
    assert roles_and_ids == [
        ("assistant", None, True),
        ("tool", "call_a", False),
        ("assistant", None, True),
        ("tool", "call_b", False),
        ("user", None, False),
    ]


def test_sanitize_tool_calls_drops_orphan_created_by_compression_boundary() -> None:
    compressed = [
        ChatMessage(role="system", content="system"),
        ChatMessage(role="user", content="compressed summary"),
        ChatMessage(role="tool", content='{"ok": true}', tool_call_id="orphaned"),
        ChatMessage(role="user", content="latest"),
    ]

    sanitized = _sanitize_tool_calls(compressed)

    assert [message.role for message in sanitized] == ["system", "user", "user"]
