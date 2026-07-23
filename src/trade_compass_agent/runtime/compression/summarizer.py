"""Phase 2: LLM-based conversation summarization.

When token budget exceeds the summary threshold (~80%), compress middle
turns of the conversation into a structured summary using the main LLM.

Design absorbed from:
- structured summary template with iterative updates
- <analysis>/<summary> two-block design with an anti-execution prefix
- Trade Compass custom: trading-domain summary focusing on stock symbols, data, signals
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from trade_compass_agent.runtime.compression.trim import _resolve_protect_tail_boundary

if TYPE_CHECKING:
    from trade_compass_agent.llm.providers import ChatMessage

logger = logging.getLogger(__name__)

_SUMMARY_CHUNK_MAX_CHARS = 120_000

# ---- Anti-execution prefix ----
# This is prepended to the summary so the LLM doesn't treat it as new instructions.
_SUMMARY_PREFIX = (
    "[上下文压缩 — 仅参考] "
    "以下为前文的结构化摘要，不是新的用户指令。"
    "不要执行摘要中提到的任务；它们已被处理。"
    "你的当前任务是最新的用户消息（在本摘要之后）。\n\n"
)

# ---- Summary prompt template ----
_SUMMARIZE_SYSTEM_PROMPT = """你是一个交易会话归档助手。你的任务是把一段 A 股分析对话
压缩成结构化的摘要。保留所有关键信息，丢弃冗余的工具输出和重复内容。

要求：
1. 准确记录用户原始意图和约束条件
2. 列出所有已查询的股票代码及其分析维度
3. 保留获取的关键数据（价格、均线、成交量、财务指标等）
4. 记录已给出的结论和信号评级
5. 记录已执行的重要工具调用及结果概要
6. 标记未完成的待办任务

用 <summary> 标签包裹你的输出，内部使用以下中文格式：

<summary>
1. 用户意图: [用户最初想做什么、有什么约束]
2. 股票池: [600519, 000858, ...]
3. 关键数据:
   - 600519 日线 60根, 最新价 ¥1680.00, MA20 ¥1655.00
   - 市场脉搏: 涨停 23家, 领涨 白酒 +2.3%
4. 信号结论:
   - 600519: 反弹择机 (+2.1%), 短线关注
5. 当前工作: [正在进行中的分析或操作]
6. 待办: [尚未完成的任务]
</summary>

注意：
- 只写事实和数据，不要添加新分析
- 用中文输出
- 股票代码用 6 位数字格式
"""


def _build_summarize_user_message(
    middle_messages: list[ChatMessage],
    previous_summary: str | None = None,
) -> str:
    """Build the user message asking the LLM to summarize middle turns.

    Args:
        middle_messages: The messages to summarize (already trimmed).
        previous_summary: Prior summary from a previous compaction,
            used for iterative update.
    """
    lines = ["请总结以下对话片段：", ""]

    if previous_summary:
        lines.append("## 前次摘要（可在此基础上增量更新）")
        lines.append(previous_summary)
        lines.append("")

    lines.append("## 待摘要的对话")
    for msg in middle_messages:
        role_label = _role_label(msg.role)
        content = msg.content or ""
        # Truncate very long tool results in the summarizer input
        if msg.role == "tool" and len(content) > 2000:
            content = content[:2000] + "…[已截断]"
        lines.append(f"{role_label}: {content}")
        # Include tool call info
        for tc in msg.tool_calls or []:
            fn = tc.get("function", {})
            name = fn.get("name", "?")
            args = fn.get("arguments", "")[:200]
            lines.append(f"  → 调用工具 {name}({args})")

    return "\n".join(lines)


def _align_head_boundary_forward(messages: list[ChatMessage], boundary: int) -> int:
    """Move a head boundary past tool results belonging to its assistant call."""
    while boundary < len(messages) and messages[boundary].role == "tool":
        boundary += 1
    return boundary


def _align_tail_boundary_backward(messages: list[ChatMessage], boundary: int) -> int:
    """Move a tail boundary to the assistant that opened a tool-result group."""
    if boundary >= len(messages) or messages[boundary].role != "tool":
        return boundary
    while boundary > 0 and messages[boundary].role == "tool":
        boundary -= 1
    return boundary


def _is_persisted_summary(message: ChatMessage) -> bool:
    return bool(message.content and message.content.startswith(_SUMMARY_PREFIX))


def latest_persisted_summary(messages: list[ChatMessage]) -> str | None:
    """Return the latest durable summary embedded in session history."""
    for message in reversed(messages):
        if _is_persisted_summary(message):
            return (message.content or "")[len(_SUMMARY_PREFIX):].strip() or None
    return None


def _atomic_message_units(messages: list[ChatMessage]) -> list[list[ChatMessage]]:
    """Group assistant tool calls and their results into indivisible units."""
    units: list[list[ChatMessage]] = []
    index = 0
    while index < len(messages):
        message = messages[index]
        unit = [message]
        index += 1
        if message.role == "assistant" and message.tool_calls:
            while index < len(messages) and messages[index].role == "tool":
                unit.append(messages[index])
                index += 1
        elif message.role == "tool":
            while index < len(messages) and messages[index].role == "tool":
                unit.append(messages[index])
                index += 1
        units.append(unit)
    return units


def _summary_input_size(messages: list[ChatMessage]) -> int:
    """Estimate rendered prompt size without constructing a potentially huge string."""
    total = 128
    for message in messages:
        content_len = len(message.content or "")
        total += min(content_len, 2000) if message.role == "tool" else content_len
        total += 32
        for tool_call in message.tool_calls or []:
            function = tool_call.get("function", {})
            total += min(len(function.get("arguments", "")), 200) + 64
    return total


def _chunk_middle_messages(messages: list[ChatMessage]) -> list[list[ChatMessage]]:
    """Split old history at safe message-group boundaries for summary calls."""
    chunks: list[list[ChatMessage]] = []
    current: list[ChatMessage] = []
    for unit in _atomic_message_units(messages):
        candidate = [*current, *unit]
        if current and _summary_input_size(candidate) > _SUMMARY_CHUNK_MAX_CHARS:
            chunks.append(current)
            current = list(unit)
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def _bounded_summary_prompt(messages: list[ChatMessage], previous_summary: str | None) -> str:
    """Render one chunk and cap plain-text summarizer input as a final safety net."""
    prompt = _build_summarize_user_message(messages, previous_summary)
    if len(prompt) <= _SUMMARY_CHUNK_MAX_CHARS:
        return prompt
    return prompt[: _SUMMARY_CHUNK_MAX_CHARS - 16] + "\n…[片段已截断]"


def _role_label(role: str) -> str:
    mapping = {
        "system": "系统",
        "user": "用户",
        "assistant": "助手",
        "tool": "工具结果",
    }
    return mapping.get(role, role)


def _count_messages_tokens_approx(messages: list[ChatMessage]) -> int:
    """Rough token estimate for summarizer's own consumption."""
    total = 0
    for msg in messages:
        total += len(msg.content or "") // 4 + 4
        for tc in msg.tool_calls or []:
            fn = tc.get("function", {})
            total += len(fn.get("arguments", "")) // 4
    return total


def summarize_middle_turns(
    messages: list[ChatMessage],
    *,
    llm_call,
    protect_recent_count: int = 20,
    protect_recent_tokens: int = 16000,
    protect_head_count: int = 2,
    previous_summary: str | None = None,
) -> tuple[list[ChatMessage], str, int, int]:
    """Compress middle conversation turns into an LLM-generated summary.

    Algorithm:
    1. Identify protected zones (system + head + tail)
    2. Extract middle messages
    3. Send middle messages to LLM for summarization
    4. Reconstruct message list: system + summary + tail

    Args:
        messages: Full message list including system prompt.
        llm_call: Function (system_prompt, user_content) -> response_text.
        protect_recent_count: Minimum tail messages to protect from summarization.
        protect_recent_tokens: Minimum tail tokens to protect from summarization.
        protect_head_count: Non-system messages to protect at head.
        previous_summary: Prior summary for iterative update across compactions.

    Returns:
        (compressed_messages, summary_text, messages_summarized, chars_saved)

        If there are fewer than ``protect_head_count + protect_recent_count``
        non-system messages, returns the original list unchanged.
    """
    n = len(messages)
    if n == 0:
        return messages, "", 0, 0

    # --- Determine boundaries ---
    # System prompt at index 0 is always protected
    head_boundary = min(1 + protect_head_count, n)
    head_boundary = _align_head_boundary_forward(messages, head_boundary)
    tail_boundary = _resolve_protect_tail_boundary(
        messages, protect_recent_count, protect_recent_tokens,
    )
    tail_boundary = _align_tail_boundary_backward(messages, tail_boundary)
    if tail_boundary <= head_boundary:
        logger.debug(
            "summarize: nothing in middle zone (head=%d tail=%d n=%d)",
            head_boundary, tail_boundary, n,
        )
        return messages, "", 0, 0

    system_msg = messages[0] if messages[0].role == "system" else None
    head_msgs = messages[1:head_boundary] if system_msg else messages[:head_boundary]
    middle_msgs = messages[head_boundary:tail_boundary]
    tail_msgs = messages[tail_boundary:]

    # A prior summary is durable session state. Reuse it, but never retain two
    # summary messages in the reconstructed context.
    previous_summary = previous_summary or latest_persisted_summary(messages)
    head_msgs = [message for message in head_msgs if not _is_persisted_summary(message)]
    summary_input_msgs = [message for message in middle_msgs if not _is_persisted_summary(message)]
    tail_msgs = [message for message in tail_msgs if not _is_persisted_summary(message)]

    # Skip summarization if middle section is too small to be worth it
    if len(summary_input_msgs) < 4:
        logger.debug("summarize: middle section too small (%d messages)", len(middle_msgs))
        return messages, "", 0, 0

    logger.info(
        "summarize: sending %d middle messages (~%d chars) to LLM for summary",
        len(summary_input_msgs),
        sum(len(m.content or "") for m in summary_input_msgs),
    )

    # --- Call LLM for summary, incrementally for oversized histories ---
    summary_text = previous_summary or ""
    for chunk in _chunk_middle_messages(summary_input_msgs):
        user_prompt = _bounded_summary_prompt(chunk, summary_text or None)
        try:
            raw_summary = llm_call(_SUMMARIZE_SYSTEM_PROMPT, user_prompt)
        except Exception as exc:
            logger.error("summarize: LLM call failed: %s", exc)
            return messages, "", 0, 0
        if not raw_summary or not raw_summary.strip():
            logger.warning("summarize: LLM returned empty summary")
            return messages, "", 0, 0
        summary_text = _extract_summary_block(raw_summary)
        if not summary_text:
            logger.warning("summarize: no <summary> block found in LLM response")
            return messages, "", 0, 0

    # --- Reconstruct message list ---
    summary_msg_content = _SUMMARY_PREFIX + summary_text

    from trade_compass_agent.llm.providers import ChatMessage

    compressed: list[ChatMessage] = []
    if system_msg:
        compressed.append(system_msg)
    compressed.extend(head_msgs)
    compressed.append(ChatMessage(role="user", content=summary_msg_content))
    # Flatten the tail messages — put the most recent user message LAST
    # so the LLM knows this is the current task
    compressed.extend(tail_msgs)

    messages_summarized = len(middle_msgs)
    chars_saved = sum(len(m.content or "") for m in middle_msgs) - len(summary_msg_content)

    logger.info(
        "summarize: %d messages → summary (%d chars), saved ~%d chars",
        messages_summarized, len(summary_text), max(0, chars_saved),
    )

    return compressed, summary_text, messages_summarized, max(0, chars_saved)


def _extract_summary_block(text: str) -> str:
    """Extract text within <summary>...</summary> tags.

    Handles both:
    - <summary>block</summary>
    - <analysis>...</analysis>\n<summary>block</summary>  (ignored wrapper)
    """
    import re

    # Match <summary>...</summary>
    m = re.search(r"<summary>(.*?)</summary>", text, re.DOTALL)
    if m:
        return m.group(1).strip()

    # Fallback: if no tags, use the whole response (truncated)
    logger.warning("No <summary> tags found in LLM response, using raw text")
    return text[:4000].strip()
