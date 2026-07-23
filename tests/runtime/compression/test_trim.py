"""Tests for trim.py — Phase 1 tool-result trimming."""

from __future__ import annotations


from trade_compass_agent.llm.providers import ChatMessage
from trade_compass_agent.runtime.compression.trim import (
    trim_tool_results,
    _summarize_tool_result,
    _resolve_protect_tail_boundary,
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


def _make_tool_call(id, name, args="{}"):
    return {"id": id, "function": {"name": name, "arguments": args}}


# ---- Tool result summarization ----

def test_summarize_get_bars():
    result = '{"symbol": "600519", "frequency": "1d", "count": 60, "bars": [{"close": 1680.00}]}'
    summary = _summarize_tool_result("get_bars", '{"symbol":"600519","frequency":"1d"}', result)
    assert "600519" in summary
    assert "1680.00" in summary
    assert "日线" in summary


def test_summarize_get_bars_no_bars():
    result = '{"error": "no data"}'
    summary = _summarize_tool_result("get_bars", '{"symbol":"600519"}', result)
    assert "600519" in summary


def test_summarize_get_market_pulse():
    result = '{"limit_up": {"count": 23}, "sectors": [{"name": "白酒", "change_pct": 2.3}]}'
    summary = _summarize_tool_result("get_market_pulse", "{}", result)
    assert "涨停 23" in summary
    assert "白酒" in summary
    assert "2.30%" in summary


def test_summarize_kline_forecast():
    result = '{"symbol": "600519", "forecast_summary": {"change_pct": 2.1, "direction": "up"}}'
    summary = _summarize_tool_result("kline_forecast", "{}", result)
    assert "600519" in summary
    assert "2.10%" in summary


def test_summarize_search_news():
    summary = _summarize_tool_result("search_news", '{"query":"白酒 政策"}', "long result " * 100)
    assert "search_news" in summary
    assert "白酒" in summary


def test_summarize_generic():
    summary = _summarize_tool_result("unknown_tool", "{}", "Some result\nline2\nline3")
    assert "[unknown_tool]" in summary


# ---- Protection zone boundaries ----

def test_protect_boundary_small_list():
    msgs = [_make_msg("user", "hi")] * 10
    boundary = _resolve_protect_tail_boundary(msgs, protect_count=20, protect_tokens=0)
    assert boundary == 0  # all protected


def test_protect_boundary_count():
    msgs = [_make_msg("user", "hello world ")] * 100
    boundary = _resolve_protect_tail_boundary(msgs, protect_count=20, protect_tokens=0)
    assert boundary == 80  # protect last 20


def test_protect_boundary_uses_larger():
    """Token budget and count — should use larger protection (count wins)."""
    msgs = [_make_msg("user", "short")] * 100
    # protect_tokens=0 → falls back to count
    boundary = _resolve_protect_tail_boundary(msgs, protect_count=20, protect_tokens=0)
    assert boundary == 80


# ---- Trimming: main function ----

def test_trim_preserves_system_and_recent():
    msgs = [
        _make_msg("system", "You are a trading assistant."),
        _make_msg("user", "hi"),
        _make_msg("assistant", "hello"),
        _make_msg("user", "check 600519"),
        _make_msg(
            "assistant", "let me check",
            tool_calls=[_make_tool_call("c1", "get_bars")],
        ),
        _make_msg(
            "tool", "x" * 3000,
            tool_call_id="c1", name="get_bars",
        ),
    ]
    trimmed, pruned, saved = trim_tool_results(
        msgs, protect_recent_count=20, protect_recent_tokens=0,
    )
    assert pruned == 0  # all within protection zone
    assert len(trimmed) == len(msgs)


def test_trim_prunes_old_tool_results():
    """Old tool results outside protection zone should be summarized."""
    msgs = [
        _make_msg("system", "You are a trading assistant."),
        _make_msg("user", "task 1"),
        _make_msg("assistant", "doing task 1", tool_calls=[_make_tool_call("c1", "search_news")]),
        _make_msg("tool", "x" * 3000, tool_call_id="c1", name="search_news"),
        # ... many messages pushing c1 outside protection ...
    ]
    # Add padding so c1 falls outside protection
    for i in range(30):
        msgs.append(_make_msg("user", f"msg {i}"))
    trimmed, pruned, saved = trim_tool_results(
        msgs, protect_recent_count=5, protect_recent_tokens=0,
    )
    assert pruned >= 1
    assert saved > 0
    # The tool message should be summarized
    tool_msgs = [m for m in trimmed if m.role == "tool"]
    summarized = [m for m in tool_msgs if "[search_news]" in m.content]
    assert len(summarized) >= 1


def test_trim_protects_protected_tools():
    """get_bars, kline_forecast, get_market_pulse should never be trimmed."""
    msgs = [
        _make_msg("system", "You are a trading assistant."),
        _make_msg("user", "check"),
        _make_msg("assistant", "ok", tool_calls=[_make_tool_call("c1", "get_bars")]),
        _make_msg("tool", "x" * 3000, tool_call_id="c1", name="get_bars"),
    ]
    for i in range(30):
        msgs.append(_make_msg("user", f"msg {i}"))
    trimmed, pruned, saved = trim_tool_results(
        msgs, protect_recent_count=3, protect_recent_tokens=0,
    )
    # get_bars tool message should still be full content
    bars_msgs = [m for m in trimmed if m.role == "tool" and m.name == "get_bars"]
    assert len(bars_msgs) >= 1
    assert "x" * 3000 in bars_msgs[0].content  # NOT truncated


def test_trim_no_messages():
    trimmed, pruned, saved = trim_tool_results([])
    assert trimmed == []
    assert pruned == 0
    assert saved == 0


def test_trim_deduplicates_identical_results():
    msgs = [
        _make_msg("system", "You are a trading assistant."),
    ]
    # Padding to push tool results outside head protection
    for i in range(5):
        msgs.append(_make_msg("user", f"padding {i}"))
        msgs.append(_make_msg("assistant", f"reply {i}"))
    # Now add duplicate tool results in the middle zone
    msgs.append(_make_msg(
        "assistant", "check", tool_calls=[_make_tool_call("c1", "search_news")],
    ))
    msgs.append(_make_msg("tool", "a" * 500, tool_call_id="c1", name="search_news"))
    msgs.append(_make_msg(
        "assistant", "check again", tool_calls=[_make_tool_call("c2", "search_news")],
    ))
    msgs.append(_make_msg("tool", "a" * 500, tool_call_id="c2", name="search_news"))
    # Pad tail so these are in middle zone
    for i in range(25):
        msgs.append(_make_msg("user", f"tail {i}"))
    trimmed, pruned, saved = trim_tool_results(
        msgs, protect_recent_count=3, protect_recent_tokens=0,
    )
    # The duplicate (earlier occurrence) should be replaced
    dupe_msgs = [m for m in trimmed if "重复" in m.content or "省略" in m.content]
    assert len(dupe_msgs) >= 1
    assert saved > 0
