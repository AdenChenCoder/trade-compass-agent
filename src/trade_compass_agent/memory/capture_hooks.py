"""Auto-capture hooks — decides what tool results to persist to Working tier.

Captures high-value events at explicit runtime boundaries. For a trading agent,
we capture significant market events, signals, and notable data points
without requiring the agent to explicitly call write_knowledge.

Design: fast synchronous check (no LLM call), fire-and-forget write.
"""

from __future__ import annotations

import json
import logging
import re

logger = logging.getLogger(__name__)

# Tools whose results are ALWAYS captured (high signal density)
ALWAYS_CAPTURE_TOOLS = frozenset({
    "emit_signal",
    "dispatch_specialist",
    "kline_forecast",
    "chart_pattern",
})

# Tools whose results are NEVER captured (noise / too frequent)
NEVER_CAPTURE_TOOLS = frozenset({
    "get_bars",
    "calculate_indicators",
    "session_search",
    "write_knowledge",
    "skill_manage",
})

# Patterns in tool results that trigger capture
CAPTURE_PATTERNS = [
    (re.compile(r"涨停.*?(\d+)", re.IGNORECASE), "limit_up_count"),
    (re.compile(r"跌停.*?(\d+)", re.IGNORECASE), "limit_down_count"),
    (re.compile(r"(net_buy|净买入|净流入).*?([+-]?\d+(?:\.\d+)?)", re.IGNORECASE), "fund_flow_signal"),
    (re.compile(r"(signal_type|direction|confidence)", re.IGNORECASE), "signal_emitted"),
    (re.compile(r"(主力|大单).*?(流入|流出).*?(\d+)", re.IGNORECASE), "main_force_flow"),
]

# Minimum result length to consider for pattern matching (Chinese is denser)
MIN_RESULT_LENGTH = 10
# Maximum result length to store as raw_preview
MAX_RAW_PREVIEW = 2000


def should_capture(tool_name: str, result: str) -> bool:
    """Decide if a tool result should be auto-captured to Working tier."""
    if tool_name in ALWAYS_CAPTURE_TOOLS:
        return True
    if tool_name in NEVER_CAPTURE_TOOLS:
        return False
    if len(result) < MIN_RESULT_LENGTH:
        return False

    # Check if result contains significant patterns
    for pattern, _ in CAPTURE_PATTERNS:
        if pattern.search(result):
            return True

    # Check for error/unavailable — don't capture those
    if "暂不可用" in result or "error" in result.lower()[:100]:
        return False

    # Check for JSON with notable fields
    try:
        data = json.loads(result)
        if isinstance(data, dict):
            if data.get("signals") or data.get("signal_type"):
                return True
    except (json.JSONDecodeError, ValueError, TypeError):
        pass

    return False


def extract_summary(tool_name: str, result: str) -> str:
    """Extract a concise summary from a tool result for storage."""
    # For signals/specialists, use the result directly (already structured)
    if tool_name in ALWAYS_CAPTURE_TOOLS:
        return _truncate(result, 500)

    # Try to parse as JSON and extract key fields
    try:
        data = json.loads(result)
        if isinstance(data, dict):
            # Prefer distilled fields over raw k=v dump
            for key in ("summary", "message", "answer", "conclusion", "insight"):
                if key in data and isinstance(data[key], str) and len(data[key]) >= 10:
                    query = data.get("query", "")
                    prefix = f"{query}: " if query else ""
                    return f"[{tool_name}] {prefix}{_truncate(data[key], 450)}"
            parts = []
            for k, v in list(data.items())[:5]:
                if isinstance(v, (str, int, float)):
                    parts.append(f"{k}={v}")
            return f"[{tool_name}] " + "; ".join(parts)[:450]
    except (json.JSONDecodeError, ValueError):
        pass

    # For plain text, extract matched patterns
    matched_labels = []
    for pattern, label in CAPTURE_PATTERNS:
        m = pattern.search(result)
        if m:
            matched_labels.append(f"{label}:{m.group(0)[:60]}")

    if matched_labels:
        return f"[{tool_name}] " + " | ".join(matched_labels)

    return f"[{tool_name}] " + _truncate(result, 400)


def _truncate(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    return text[:max_len - 3] + "..."
