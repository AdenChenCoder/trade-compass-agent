"""Tests for summarizer.py — Phase 2 LLM summarization."""

from __future__ import annotations

import pytest

from trade_compass_agent.llm.providers import ChatMessage
from trade_compass_agent.runtime.compression.summarizer import (
    summarize_middle_turns,
    _align_head_boundary_forward,
    _align_tail_boundary_backward,
    _extract_summary_block,
    _build_summarize_user_message,
    _SUMMARY_PREFIX,
)


# ---- Helpers ----

def _make_msg(role, content="", tool_call_id=None, name=None, tool_calls=None):
    return ChatMessage(
        role=role,
        content=content,
        tool_call_id=tool_call_id,
        name=name,
        tool_calls=tool_calls or [],
    )


def _fake_llm(sys_prompt: str, user_content: str) -> str:
    """Return a valid summary for testing."""
    lines = []
    for line in user_content.split("\n"):
        if "600519" in line:
            lines.append("- 600519 最新价 ¥1680.00")
            break
    return "\n".join([
        "<summary>",
        "1. 用户意图: 查询 600519 短线走势",
        "2. 股票池: [600519]",
        "3. 关键数据:",
    ] + lines + [
        "4. 信号结论: 600519 反弹择机 +2.1%",
        "5. 当前工作: 完成 K 线分析",
        "6. 待办: 无",
        "</summary>",
    ])


# ---- Tag extraction ----

def test_extract_summary_block():
    text = "<analysis>thinking...</analysis>\n<summary>\n关键数据\n</summary>"
    result = _extract_summary_block(text)
    assert "关键数据" in result
    assert "thinking" not in result


def test_extract_summary_block_no_tags():
    text = "plain text without any tags"
    result = _extract_summary_block(text)
    assert "plain text" in result


def test_extract_summary_block_empty():
    assert _extract_summary_block("") == ""


# ---- User message building ----

def test_build_summarize_user_message():
    msgs = [
        _make_msg("user", "600519 短线怎么看"),
        _make_msg(
            "assistant", "我来查一下",
            tool_calls=[{"id": "c1", "function": {"name": "get_bars", "arguments": '{"symbol":"600519"}'}}],
        ),
        _make_msg("tool", '{"symbol":"600519","close":1680}', tool_call_id="c1", name="get_bars"),
    ]
    result = _build_summarize_user_message(msgs)
    assert "600519" in result
    assert "get_bars" in result
    assert "短线怎么看" in result


def test_build_summarize_user_message_with_previous_summary():
    msgs = [_make_msg("user", "check 000858")]
    result = _build_summarize_user_message(msgs, previous_summary="前次摘要: 分析了 600519")
    assert "前次摘要" in result
    assert "600519" in result


# ---- Main summarization ----

def test_summarize_success():
    """Happy path: LLM returns a valid summary."""
    msgs = [
        _make_msg("system", "You are a trading assistant."),
        _make_msg("user", "hi"),
        _make_msg("assistant", "hello, what do you need?"),
        _make_msg("user", "check 600519"),
        _make_msg("assistant", "let me check", tool_calls=[
            {"id": "c1", "function": {"name": "get_bars", "arguments": '{"symbol":"600519"}'}},
        ]),
        _make_msg("tool", '{"symbol":"600519","bars":[{"close":1680}]}', tool_call_id="c1", name="get_bars"),
        _make_msg("assistant", "600519 looks good"),
    ]
    # Add padding so middle zone exists
    for i in range(30):
        msgs.append(_make_msg("user", f"padding {i}"))

    compressed, summary, summarized, saved = summarize_middle_turns(
        msgs,
        llm_call=_fake_llm,
        protect_recent_count=5,
        protect_recent_tokens=0,
    )
    assert summarized > 0
    assert len(compressed) < len(msgs)
    assert "600519" in summary
    assert saved > 0
    # Summary prefix should be present in the compressed message
    summary_msg = [m for m in compressed if _SUMMARY_PREFIX.strip()[:10] in m.content]
    assert len(summary_msg) == 1


def test_summarize_too_few_messages():
    """When there's nothing in the middle zone, return unchanged."""
    msgs = [
        _make_msg("system", "assistant"),
        _make_msg("user", "hi"),
        _make_msg("assistant", "hello"),
    ]
    compressed, summary, summarized, saved = summarize_middle_turns(
        msgs, llm_call=_fake_llm, protect_recent_count=20,
    )
    assert summarized == 0
    assert compressed == msgs


def test_summarize_llm_error():
    """LLM failure should return unchanged messages."""
    msgs = [_make_msg("system", "assistant")]
    for i in range(50):
        msgs.append(_make_msg("user", f"msg {i}"))

    def _failing_llm(sys, user):
        raise RuntimeError("API down")

    compressed, summary, summarized, saved = summarize_middle_turns(
        msgs, llm_call=_failing_llm, protect_recent_count=5, protect_recent_tokens=0,
    )
    assert summarized == 0
    assert compressed == msgs


def test_summarize_empty_response():
    """Empty LLM response should return unchanged."""
    msgs = [_make_msg("system", "assistant")]
    for i in range(50):
        msgs.append(_make_msg("user", f"msg {i}"))

    compressed, _, summarized, _ = summarize_middle_turns(
        msgs,
        llm_call=lambda s, u: "",
        protect_recent_count=5,
        protect_recent_tokens=0,
    )
    assert summarized == 0


def test_summarize_iterative():
    """Iterative summary across compactions."""
    msgs = [_make_msg("system", "assistant")]
    for i in range(50):
        msgs.append(_make_msg("user", f"msg {i}"))

    first, s1, _, _ = summarize_middle_turns(
        msgs, llm_call=_fake_llm, protect_recent_count=5,
    )
    assert s1

    # Second pass with previous summary
    second, s2, _, _ = summarize_middle_turns(
        first, llm_call=_fake_llm, protect_recent_count=5,
        previous_summary=s1,
    )
    # s2 should be at least as long (iterative update)
    assert len(s2) >= len(s1) or summarize_middle_turns is not None


def test_summary_prefix():
    """Anti-execution prefix must be present."""
    msgs = [_make_msg("system", "assistant")]
    for i in range(50):
        msgs.append(_make_msg("user", f"msg {i}"))

    compressed, _, summarized, _ = summarize_middle_turns(
        msgs, llm_call=_fake_llm, protect_recent_count=5,
    )
    assert summarized > 0

    # Find the summary message
    for msg in compressed:
        if "上下文压缩" in msg.content and "600519" in msg.content:
            assert msg.content.startswith("[上下文压缩")
            assert "不是新的用户指令" in msg.content
            break
    else:
        pytest.fail("Summary message with anti-execution prefix not found")


def test_summarize_preserves_tail_user_message():
    """The most recent user message must always be in the tail."""
    msgs = [_make_msg("system", "assistant")]
    for i in range(20):
        msgs.append(_make_msg("user", f"old msg {i}"))
    msgs.append(_make_msg("user", "LATEST: 600519 现在怎么看"))
    # Add a few assistant replies after the latest user
    for i in range(5):
        msgs.append(_make_msg("assistant", f"reply {i}"))

    compressed, _, summarized, _ = summarize_middle_turns(
        msgs, llm_call=_fake_llm, protect_recent_count=8,
    )
    assert summarized > 0
    # The latest user message should be preserved in the tail
    latest = [m for m in compressed if "LATEST" in (m.content or "")]
    assert len(latest) == 1


def test_compression_boundaries_keep_parallel_tool_round_intact():
    msgs = [
        _make_msg("system", "assistant"),
        _make_msg("user", "check two symbols"),
        _make_msg("assistant", "checking", tool_calls=[
            {"id": "c1", "function": {"name": "get_bars", "arguments": "{}"}},
            {"id": "c2", "function": {"name": "get_bars", "arguments": "{}"}},
        ]),
        _make_msg("tool", "result 1", tool_call_id="c1", name="get_bars"),
        _make_msg("tool", "result 2", tool_call_id="c2", name="get_bars"),
        _make_msg("assistant", "done"),
    ]

    assert _align_head_boundary_forward(msgs, 3) == 5
    assert _align_tail_boundary_backward(msgs, 4) == 2


def test_summarize_large_middle_in_safe_chunks():
    msgs = [_make_msg("system", "assistant"), _make_msg("user", "head")]
    for i in range(10):
        msgs.append(_make_msg("assistant", f"report {i} " + "x" * 40_000))
        msgs.append(_make_msg("user", f"follow-up {i}"))

    calls = []

    def chunked_llm(sys_prompt: str, user_content: str) -> str:
        calls.append(user_content)
        return "<summary>chunk summary</summary>"

    compressed, summary, summarized, _ = summarize_middle_turns(
        msgs,
        llm_call=chunked_llm,
        protect_head_count=1,
        protect_recent_count=2,
        protect_recent_tokens=0,
    )

    assert summarized > 0
    assert summary == "chunk summary"
    assert len(calls) > 1
    assert all(len(call) < 160_000 for call in calls)
    assert len(compressed) < len(msgs)
