"""Smart tool-result trimming — Phase 1 compression (no LLM call).

Replaces the legacy ``_trim_old_tool_results`` which hard-truncated
all old tool results to 800 characters.  This module provides:

1. Protection zones (system prompt, head, tail by token budget)
2. Structured tool-result summaries (preserving key data)
3. Tool-call argument truncation
4. Duplicate tool-result deduplication

All operations are cheap — pure CPU, no network calls.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from trade_compass_agent.llm.providers import ChatMessage

logger = logging.getLogger(__name__)

# ---- Placeholders ----
_PRUNED_PLACEHOLDER = "[工具结果已截断]"
_DUPE_PLACEHOLDER = "[与更近的调用结果重复 — 内容已省略]"

# ---- Configurable thresholds ----
_LARGE_CONTENT_CHARS = 2000    # only trim tool results larger than this
_LARGE_ARGS_CHARS = 2000       # only truncate tool_call args larger than this
_ARGS_HEAD_CHARS = 200         # keep first N chars of truncated args

# ---- Tools that should NEVER lose their data in trimming ----
# Market data tools are critical for trading decisions.
_PROTECTED_TOOLS: set[str] = {
    "get_bars",
    "kline_forecast",
    "get_market_pulse",
}


def _build_tool_index(messages: list[ChatMessage]) -> dict[str, tuple[str, str]]:
    """Build call_id → (tool_name, arguments_json) index from assistant messages."""
    index: dict[str, tuple[str, str]] = {}
    for msg in messages:
        if msg.role != "assistant":
            continue
        for tc in msg.tool_calls or []:
            fn = tc.get("function", {})
            cid = tc.get("id", "")
            if cid:
                index[cid] = (fn.get("name", "unknown"), fn.get("arguments", ""))
    return index


def _summarize_tool_result(tool_name: str, tool_args: str, content: str) -> str:
    """Generate a compact, informative summary of a tool call + result.

    Returns strings like::

        [get_bars] 600519 日线 × 60 根, latest close ¥1680.00
        [get_market_pulse] 涨停 23 家, 领涨 白酒 +2.3%
        [search_news] '白酒 政策' → 8 results
    """
    try:
        args = json.loads(tool_args) if tool_args else {}
    except (json.JSONDecodeError, TypeError):
        args = {}

    content = content or ""
    content_len = len(content)

    # ---- Market data tools: parse key facts ----
    if tool_name == "get_bars":
        symbol = str(args.get("symbol", "") or "")
        freq = str(args.get("frequency", "") or "")
        count = args.get("count", "")
        count_text = f" × {count}" if count else ""
        try:
            payload = json.loads(content)
            if isinstance(payload, dict) and not payload.get("error"):
                bars = payload.get("bars") or []
                if bars:
                    latest = bars[-1]
                    close = latest.get("close")
                    if close is not None:
                        freq_label = _freq_label(freq)
                        return f"[get_bars] {symbol} {freq_label}{count_text}, latest close ¥{close:.2f}"
        except (json.JSONDecodeError, TypeError):
            pass
        return f"[get_bars] {symbol} {freq}{count_text} ({content_len:,} chars)"

    if tool_name == "kline_forecast":
        try:
            payload = json.loads(content)
            if isinstance(payload, dict):
                symbol = str(payload.get("symbol") or "")
                fcast = payload.get("forecast_summary") or {}
                change_pct = fcast.get("change_pct", 0)
                direction = fcast.get("direction", "sideways")
                dir_label = {"up": "↑", "down": "↓", "sideways": "→"}.get(direction, direction)
                return f"[kline_forecast] {symbol} {dir_label} {change_pct:+.2f}%"
        except (json.JSONDecodeError, TypeError):
            pass
        return f"[kline_forecast] ({content_len:,} chars)"

    if tool_name == "get_market_pulse":
        try:
            payload = json.loads(content)
            if isinstance(payload, dict):
                limit_up = payload.get("limit_up") or {}
                lu_count = limit_up.get("count", "?")
                sectors = payload.get("sectors") or []
                parts = [f"涨停 {lu_count} 家"]
                if sectors:
                    top = sectors[0]
                    name = top.get("name")
                    pct = top.get("change_pct")
                    if name and pct is not None:
                        parts.append(f"领涨 {name} {pct:+.2f}%")
                return f"[get_market_pulse] {', '.join(parts)}"
        except (json.JSONDecodeError, TypeError):
            pass
        return f"[get_market_pulse] ({content_len:,} chars)"

    # ---- General tool summarization ----
    if tool_name in {"search_news", "search_web"}:
        query = str(args.get("query", "") or "")
        if len(query) > 40:
            query = query[:37] + "..."
        return f"[{tool_name}] '{query}' ({content_len:,} chars)"

    if tool_name == "get_financials":
        symbol = str(args.get("symbol", "") or "")
        try:
            payload = json.loads(content)
            if isinstance(payload, dict) and not payload.get("error"):
                pe = payload.get("pe_ttm", "?")
                roe = payload.get("roe", "?")
                return f"[get_financials] {symbol} PE={pe}, ROE={roe}"
        except (json.JSONDecodeError, TypeError):
            pass
        return f"[get_financials] {symbol} ({content_len:,} chars)"

    if tool_name in {"write_knowledge", "memory_write", "skill_manage"}:
        action = args.get("action", "?")
        return f"[{tool_name}] action={action}"

    # Generic fallback: first line summary
    first_line = content.split("\n", 1)[0][:120].strip()
    return f"[{tool_name}] {first_line}… ({content_len:,} chars)"


def _freq_label(freq: str) -> str:
    """Human-readable frequency label."""
    mapping = {
        "1d": "日线", "daily": "日线",
        "1w": "周线", "weekly": "周线",
        "1M": "月线", "monthly": "月线",
        "60m": "60分钟", "30m": "30分钟", "15m": "15分钟", "5m": "5分钟",
        "1m": "1分钟",
    }
    return mapping.get(freq, freq)


def _content_length(content: str) -> int:
    """Return effective length of message content for budget decisions."""
    if content is None:
        return 0
    return len(content)


def _truncate_tool_args(args_json: str) -> str:
    """Shrink large JSON string values in tool-call arguments while preserving
    JSON validity. Non-string values (numbers, booleans) are preserved intact.
    """
    try:
        parsed = json.loads(args_json)
    except (json.JSONDecodeError, TypeError):
        return args_json

    def _shrink(obj):
        if isinstance(obj, str) and len(obj) > _ARGS_HEAD_CHARS:
            return obj[:_ARGS_HEAD_CHARS] + "…[已截断]"
        if isinstance(obj, dict):
            return {k: _shrink(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [_shrink(v) for v in obj]
        return obj

    return json.dumps(_shrink(parsed), ensure_ascii=False)


def _resolve_protect_tail_boundary(
    messages: list[ChatMessage],
    protect_count: int,
    protect_tokens: int,
) -> int:
    """Determine how many messages from the end to protect from trimming.

    Uses the larger of token-budget protection and message-count protection.
    Returns the index of the first protected message (0-based).
    """
    n = len(messages)
    min_protected = min(protect_count, n)
    if protect_tokens <= 0:
        return n - min_protected

    accumulated = 0
    boundary = n
    for i in range(n - 1, -1, -1):
        msg = messages[i]
        msg_tokens = len(msg.content) // 4 + 10
        for tc in msg.tool_calls or []:
            fn = tc.get("function", {})
            msg_tokens += len(fn.get("arguments", "")) // 4
        if accumulated + msg_tokens > protect_tokens and (n - i) >= min_protected:
            boundary = i + 1
            break
        accumulated += msg_tokens
        boundary = i
    return max(boundary, n - min_protected)


def trim_tool_results(
    messages: list[ChatMessage],
    *,
    protect_recent_count: int = 20,
    protect_recent_tokens: int = 16000,
    protect_head_count: int = 2,
) -> tuple[list[ChatMessage], int, int]:
    """Smart trimming of old tool results.

    Returns ``(trimmed_messages, pruned_count, estimated_savings_chars)``.

    Algorithm (in order):
    1. Build tool-call ID → (name, args) index
    2. Determine tail protection boundary
    3. Pass 1: Deduplicate identical tool results
    4. Pass 2: Replace old tool results with structured summaries
    5. Pass 3: Truncate large tool-call arguments outside protected zone

    Protected zones:
    - System prompt (first message): always preserved
    - Head: first ``protect_head_count`` non-system messages preserved
    - Tail: larger of token budget or message count protection
    - PROTECTED_TOOLS: get_bars, kline_forecast, get_market_pulse always preserved
    """
    if not messages:
        return messages, 0, 0

    n = len(messages)
    # --- Determine boundaries ---
    # Skip system message (index 0) — always protected
    head_boundary = min(1 + protect_head_count, n)
    tail_boundary = _resolve_protect_tail_boundary(
        messages, protect_recent_count, protect_recent_tokens,
    )
    # Middle zone: [head_boundary, tail_boundary)
    if tail_boundary <= head_boundary:
        tail_boundary = head_boundary

    # --- Build tool index ---
    tool_index = _build_tool_index(messages)

    # Work on a shallow copy via new ChatMessage creation
    result: list[ChatMessage] = []
    pruned = 0
    savings_chars = 0

    # -- Pass 0: Identify duplicates for Pass 1 --
    content_hashes: dict[str, int] = {}  # hash → first occurrence index
    duplicate_at: set[int] = set()

    # Scan backward to keep the most recent copy
    for i in range(n - 1, -1, -1):
        msg = messages[i]
        if msg.role != "tool":
            continue
        content = msg.content or ""
        if len(content) < 200:
            continue
        h = hashlib.md5(content.encode("utf-8", errors="replace")).hexdigest()[:12]
        if h in content_hashes:
            duplicate_at.add(i)
        else:
            content_hashes[h] = i

    # --- Main loop ---
    for i, msg in enumerate(messages):
        # System message: always pass through
        if i == 0:
            result.append(msg)
            continue

        # Within head or tail protection zone: keep as-is
        if i < head_boundary or i >= tail_boundary:
            result.append(msg)
            continue

        # --- Middle zone: apply trimming ---

        # Pass 1: Deduplicate identical tool results
        if msg.role == "tool" and i in duplicate_at:
            result.append(
                msg.__class__(
                    role=msg.role,
                    content=_DUPE_PLACEHOLDER,
                    tool_call_id=msg.tool_call_id,
                    name=msg.name,
                    tool_calls=msg.tool_calls,
                )
            )
            pruned += 1
            savings_chars += _content_length(msg.content or "") - len(_DUPE_PLACEHOLDER)
            continue

        # Pass 2: Summarize large tool results
        if msg.role == "tool" and _content_length(msg.content or "") > _LARGE_CONTENT_CHARS:
            cid = msg.tool_call_id or ""
            tool_name, tool_args = tool_index.get(cid, ("unknown", ""))
            if tool_name in _PROTECTED_TOOLS:
                result.append(msg)
                continue
            summary = _summarize_tool_result(tool_name, tool_args, msg.content or "")
            result.append(
                msg.__class__(
                    role=msg.role,
                    content=summary,
                    tool_call_id=msg.tool_call_id,
                    name=msg.name,
                    tool_calls=msg.tool_calls,
                )
            )
            pruned += 1
            savings_chars += _content_length(msg.content or "") - len(summary)
            continue

        # Pass 3: Truncate large tool-call args in assistant messages
        if msg.role == "assistant" and msg.tool_calls:
            modified = False
            new_tcs = []
            for tc in msg.tool_calls:
                fn = tc.get("function", {})
                args = fn.get("arguments", "")
                if len(args) > _LARGE_ARGS_CHARS:
                    new_args = _truncate_tool_args(args)
                    if new_args != args:
                        new_fn = {**fn, "arguments": new_args}
                        new_tcs.append({**tc, "function": new_fn})
                        modified = True
                        savings_chars += len(args) - len(new_args)
                    else:
                        new_tcs.append(tc)
                else:
                    new_tcs.append(tc)
            if modified:
                result.append(
                    msg.__class__(
                        role=msg.role,
                        content=msg.content,
                        tool_calls=new_tcs,
                        tool_call_id=msg.tool_call_id,
                        name=msg.name,
                    )
                )
                pruned += 1
                continue

        result.append(msg)

    return result, pruned, savings_chars
