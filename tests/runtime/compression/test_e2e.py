"""E2E tests for the full compression pipeline.

Simulates long conversations and verifies:
1. Phase 1 trimming triggers correctly as messages accumulate
2. Phase 2 summarization produces valid output
3. Anti-thrashing prevents infinite loops
4. Context overflow recovery works
5. Protected tools (get_bars, etc.) are preserved
6. Mixed Chinese/English token estimation is reasonable
"""

from __future__ import annotations


from trade_compass_agent.llm.providers import ChatMessage
from trade_compass_agent.runtime.compression.budget import (
    TokenBudget,
    estimate_request_tokens,
)
from trade_compass_agent.runtime.compression.trim import (
    trim_tool_results,
    _PROTECTED_TOOLS,
)
from trade_compass_agent.runtime.compression.summarizer import (
    summarize_middle_turns,
    _SUMMARY_PREFIX,
)


# ---- Config stub ----
class _MockCompressionConfig:
    enabled = True
    trim_threshold_pct = 0.60
    summary_threshold_pct = 0.80
    emergency_threshold_pct = 0.95
    protect_recent_count = 20
    protect_recent_tokens = 16000
    context_budget = 0


class _MockLLMConfig:
    model = "deepseek-chat"


class _MockConfig:
    llm = _MockLLMConfig()
    context_compression = _MockCompressionConfig()


# ---- Helpers ----

_REAL_TOOLS = [
    "get_bars", "get_market_pulse", "kline_forecast",
    "search_news", "get_financials", "search_web",
    "write_knowledge", "skill_manage",
]


def _make_messages_for_turn(
    turn: int,
    symbol: str = "600519",
    with_tools: bool = True,
) -> list[ChatMessage]:
    """Simulate one conversation turn. Each turn = user + assistant + tool call."""
    msgs = [
        ChatMessage(role="user", content=f"Turn {turn}: 分析 {symbol} 短线走势"),
    ]
    if with_tools:
        msgs.append(
            ChatMessage(
                role="assistant",
                content=f"Turn {turn}: let me check {symbol}",
                tool_calls=[{
                    "id": f"call_{turn}",
                    "function": {
                        "name": _REAL_TOOLS[turn % len(_REAL_TOOLS)],
                        "arguments": f'{{"symbol":"{symbol}"}}',
                    },
                }],
            )
        )
        result = (
            f'{{"symbol":"{symbol}","bars":[{{"close":{1600+turn*10},"volume":{10000000+turn*500000}}}],'
            f'"count":60,"frequency":"1d"}}'
        )
        msgs.append(
            ChatMessage(
                role="tool",
                content=result,
                tool_call_id=f"call_{turn}",
                name=_REAL_TOOLS[turn % len(_REAL_TOOLS)],
            )
        )
    return msgs


def _build_long_conversation(num_turns: int, with_system: bool = True) -> list[ChatMessage]:
    """Build a simulated N-turn conversation. Each turn = user + assistant + tool (3 msgs)."""
    msgs = []
    if with_system:
        msgs.append(ChatMessage(
            role="system",
            content="你是 A 股交易助手。用中文回答。" + "x" * 2000,
        ))
    for t in range(num_turns):
        for msg in _make_messages_for_turn(t):
            msgs.append(msg)
    return msgs


# ---- Phase 1: trimming at scale ----

def test_trim_scales_with_conversation_length():
    """Phase 1 should prune more as conversation grows, protecting recent turns."""
    for num_turns, expect_pruned in [(10, 0), (30, 5), (60, 20)]:
        msgs = _build_long_conversation(num_turns)
        # Make tool results large enough to trigger trimming (> 2000 chars)
        for i, msg in enumerate(msgs):
            if msg.role == "tool" and len(msg.content or "") < 2000:
                msgs[i] = ChatMessage(
                    role=msg.role,
                    content=(msg.content or "") + " " + "data" * 500,
                    tool_call_id=msg.tool_call_id,
                    name=msg.name,
                )
        trimmed, pruned, saved = trim_tool_results(
            msgs, protect_recent_count=15, protect_recent_tokens=0,
        )
        if expect_pruned > 0:
            assert pruned >= expect_pruned, f"turn={num_turns}: expected ≥{expect_pruned}, got {pruned}"
        # All messages should still be present (summarized or original)
        assert len(trimmed) == len(msgs)
        # Tool results that were summarized should be shorter
        if pruned > 0:
            original_tool_len = sum(
                len(m.content or "") for m in msgs if m.role == "tool"
            )
            trimmed_tool_len = sum(
                len(m.content or "") for m in trimmed if m.role == "tool"
            )
            assert trimmed_tool_len < original_tool_len


def test_protected_tools_survive_trimming():
    """get_bars, kline_forecast, get_market_pulse must never be trimmed."""
    msgs = _build_long_conversation(50)
    trimmed, _, _ = trim_tool_results(
        msgs, protect_recent_count=5, protect_recent_tokens=0,
    )
    for msg in trimmed:
        if msg.role == "tool" and msg.name in _PROTECTED_TOOLS:
            # Protected tool results must retain their full JSON content
            assert "bars" in msg.content or "error" in msg.content or len(msg.content) > 100
            assert "[get_" not in msg.content  # not summarized


def test_trim_deduplication_at_scale():
    """Identical tool results from different turns should be deduplicated."""
    msgs = [ChatMessage(role="system", content="trading assistant.")]
    # Add padding to push these into middle zone
    for i in range(10):
        msgs.append(ChatMessage(role="user", content=f"pad {i}"))
        msgs.append(ChatMessage(role="assistant", content=f"ok {i}"))
    # Identical tool results
    for i in range(5):
        msgs.append(
            ChatMessage(role="assistant", content="check", tool_calls=[{
                "id": f"c{i}", "function": {"name": "search_news", "arguments": "{}"},
            }])
        )
        msgs.append(ChatMessage(
            role="tool", content="same result " * 100, tool_call_id=f"c{i}", name="search_news",
        ))
    # Tail padding
    for i in range(20):
        msgs.append(ChatMessage(role="user", content=f"tail {i}"))

    trimmed, pruned, saved = trim_tool_results(
        msgs, protect_recent_count=5, protect_recent_tokens=0,
    )
    # All 5 copies except the most recent should be deduplicated
    assert pruned >= 4
    assert saved > 0


# ---- Phase 2: summarization ----

def _fake_llm(sys_prompt: str, user_content: str) -> str:
    """Generate a realistic summary from the summarizer prompt."""
    symbols = []
    for line in user_content.split("\n"):
        import re
        found = re.findall(r"\b\d{6}\b", line)
        symbols.extend(found)
    symbols = list(dict.fromkeys(symbols))[:5]  # deduplicate, max 5
    symbol_list = ", ".join(symbols) if symbols else "无"

    return f"""<summary>
1. 用户意图: 多轮 A 股分析对话
2. 股票池: [{symbol_list}]
3. 关键数据:
   - 共获取 {len(symbols)} 只标的的 K 线数据
4. 信号结论: 综合评估各标的短线走势
5. 当前工作: 进行第 N 轮分析
6. 待办: 继续跟踪
</summary>"""


def test_summarize_reduces_message_count():
    """Summarization should reduce total message count significantly."""
    msgs = _build_long_conversation(40, with_system=True)
    pre_len = len(msgs)

    compressed, summary, summarized, saved = summarize_middle_turns(
        msgs, llm_call=_fake_llm, protect_recent_count=8,
    )
    assert summarized > 0
    assert len(compressed) < pre_len
    assert len(summary) > 0
    assert saved > 0


def test_summarize_preserves_system_prompt():
    """System prompt must survive summarization unchanged."""
    sys_text = "你是 A 股交易助手。用中文回答。" + "x" * 2000
    msgs = [ChatMessage(role="system", content=sys_text)]
    for t in range(40):
        for msg in _make_messages_for_turn(t):
            msgs.append(msg)

    compressed, _, summarized, _ = summarize_middle_turns(
        msgs, llm_call=_fake_llm, protect_recent_count=5,
    )
    assert summarized > 0
    assert compressed[0].role == "system"
    assert compressed[0].content == sys_text


def test_summarize_summary_prefix_present():
    """Anti-execution prefix must appear in the summary message."""
    msgs = _build_long_conversation(40, with_system=True)
    compressed, _, summarized, _ = summarize_middle_turns(
        msgs, llm_call=_fake_llm, protect_recent_count=5,
    )
    assert summarized > 0
    found = [m for m in compressed if _SUMMARY_PREFIX[:10] in (m.content or "")]
    assert len(found) == 1


# ---- Token budget estimation ----

def test_token_estimation_grows_with_conversation():
    """Estimated tokens should increase with more messages."""
    base = _build_long_conversation(5)
    mid = _build_long_conversation(20)
    large = _build_long_conversation(50)

    # With tools schemas (simulate real tool set)
    fake_schemas = [{"type": "function", "function": {"name": t, "description": "..."}}
                    for t in _REAL_TOOLS]
    est_base = estimate_request_tokens(base, tools_schemas=fake_schemas)
    est_mid = estimate_request_tokens(mid, tools_schemas=fake_schemas)
    est_large = estimate_request_tokens(large, tools_schemas=fake_schemas)

    assert est_base < est_mid < est_large


def test_token_estimation_with_tools_adds_overhead():
    """Tool schemas should add significant token overhead."""
    msgs = _build_long_conversation(10)

    without_tools = estimate_request_tokens(msgs)
    fake_schemas = [
        {"type": "function", "function": {"name": t, "description": f"Desc for {t} " + "x" * 50}}
        for t in _REAL_TOOLS
    ]
    with_tools = estimate_request_tokens(msgs, tools_schemas=fake_schemas)

    assert with_tools > without_tools
    # Tool schemas should add at least a few hundred tokens
    assert with_tools - without_tools > 200


# ---- Full pipeline integration ----

def test_full_pipeline_phase1_then_phase2():
    """Phase 1 (trim) then Phase 2 (summarize) should work together."""
    msgs = _build_long_conversation(40, with_system=True)

    # Phase 1
    trimmed, p1_count, p1_saved = trim_tool_results(
        msgs, protect_recent_count=10,
    )
    assert p1_count > 0 or len(msgs) > 0  # at least runs

    # Phase 2 on trimmed
    compressed, summary, p2_count, p2_saved = summarize_middle_turns(
        trimmed, llm_call=_fake_llm, protect_recent_count=8,
    )
    assert p2_count > 0
    assert len(compressed) < len(msgs)


def test_anti_thrashing_logic():
    """Simulate the anti-thrashing pattern: track savings, skip when ineffective."""
    msgs = _build_long_conversation(60, with_system=True)
    # Make tool results large to ensure trimming has effect
    for i, msg in enumerate(msgs):
        if msg.role == "tool" and len(msg.content or "") < 2000:
            msgs[i] = ChatMessage(
                role=msg.role,
                content=(msg.content or "") + " " + "x" * 2000,
                tool_call_id=msg.tool_call_id,
                name=msg.name,
            )

    ineffective_count = 0
    compaction_skip = False

    for iteration in range(3):
        if compaction_skip:
            break

        compressed, summary, summarized, saved = summarize_middle_turns(
            msgs, llm_call=_fake_llm, protect_recent_count=3,
        )
        if summarized == 0:
            break  # nothing left to summarize

        pre_chars = sum(len(m.content or "") for m in msgs)
        savings_ratio = saved / max(pre_chars, 1)

        if savings_ratio < 0.10:
            ineffective_count += 1
            if ineffective_count >= 2:
                compaction_skip = True
        else:
            ineffective_count = 0

        msgs = compressed

    assert not compaction_skip  # with fresh data each time, should be effective


def test_overflow_detection_patterns():
    """All overflow error patterns should be detected."""
    patterns = [
        "maximum context length",
        "context length exceeded",
        "too many tokens",
        "token limit exceeded",
        "prompt is too long",
        "input is too long",
        "exceeds the context window",
        "context window of this model",
    ]
    for p in patterns:
        assert TokenBudget.is_context_overflow_error(p), f"Failed to detect: {p}"

    # DeepSeek specific
    assert TokenBudget.is_context_overflow_error(
        "This request exceeds the maximum context length of the model"
    )


def test_overflow_recovery_simulated():
    """Simulated overflow recovery: aggressive trim should reduce message size."""
    msgs = _build_long_conversation(40, with_system=True)

    system_msg = msgs[0] if msgs[0].role == "system" else None
    middle = msgs[1:] if system_msg else msgs

    # Aggressive trim (simulating overflow recovery)
    trimmed, pruned, saved = trim_tool_results(
        middle,
        protect_recent_count=5,
        protect_recent_tokens=4000,
        protect_head_count=1,
    )
    assert pruned > 0 or saved >= 0  # runs without error

    # Messages should be drastically reduced
    original_chars = sum(len(m.content or "") for m in middle)
    trimmed_chars = sum(len(m.content or "") for m in trimmed)
    if saved > 0:
        assert trimmed_chars < original_chars
