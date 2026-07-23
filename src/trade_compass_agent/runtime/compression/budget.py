"""Context compression — token budget estimation and threshold decisions.

TokenBudget is the entry point for Phase 0 preflight checks.
It estimates token counts from message lists and decides whether
trimming (Phase 1) or summarization (Phase 2) is needed.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from trade_compass_agent.llm.providers import ChatMessage

logger = logging.getLogger(__name__)

# ---- Character-type classification for mixed Chinese/English text ----
_CJK_RANGES: list[tuple[int, int]] = [
    (0x4E00, 0x9FFF),   # CJK Unified
    (0x3400, 0x4DBF),   # CJK Unified Extension A
    (0x20000, 0x2A6DF), # CJK Unified Extension B
    (0xF900, 0xFAFF),   # CJK Compatibility
    (0x3000, 0x303F),   # CJK Symbols
    (0xFF00, 0xFFEF),   # Half-width/Full-width
    (0x3040, 0x309F),   # Hiragana
    (0x30A0, 0x30FF),   # Katakana
    (0xAC00, 0xD7AF),   # Hangul
]

# ---- Model context window sizes (fallback when config doesn't specify) ----
_MODEL_CONTEXTS: dict[str, int] = {
    "deepseek-chat": 128_000,
    "deepseek-reasoner": 64_000,
    "deepseek-v3": 128_000,
    "deepseek-v4-pro": 128_000,
    "gpt-4o": 128_000,
    "gpt-4o-mini": 128_000,
    "gpt-4-turbo": 128_000,
    "claude-sonnet-4": 200_000,
    "claude-opus-4": 200_000,
    "claude-haiku-4": 200_000,
    "claude-3-5-sonnet": 200_000,
    "claude-3-5-haiku": 200_000,
    "qwen-max": 32_000,
    "qwen-plus": 131_072,
    "qwen-turbo": 1_000_000,
    "gemini-2.5-pro": 1_048_576,
    "gemini-2.5-flash": 1_048_576,
}


def _is_cjk(cp: int) -> bool:
    for lo, hi in _CJK_RANGES:
        if lo <= cp <= hi:
            return True
    return False


def estimate_tokens(text: str, chars_per_cjk: float = 1.5, chars_per_ascii: float = 4.0) -> int:
    """Rough token estimate for mixed Chinese/English text.

    Chinese characters cost ~1 token per 1.5 chars (BPE tokenizer).
    ASCII text costs ~1 token per 4 chars.
    This is a heuristic: off by ±20% but fast and deterministic.
    """
    if not text:
        return 0
    cjk_chars = 0
    ascii_chars = 0
    for ch in text:
        if _is_cjk(ord(ch)):
            cjk_chars += 1
        else:
            ascii_chars += 1
    return int(cjk_chars / chars_per_cjk + ascii_chars / chars_per_ascii)


def estimate_message_tokens(msg: ChatMessage, chars_per_token: float = 4.0) -> int:
    """Estimate token cost of a single ChatMessage.

    Includes:
    - content text
    - tool_calls JSON (function.name + function.arguments)
    - role label overhead (~4 tokens)
    """
    tokens = estimate_tokens(msg.content) + 4  # role overhead
    for tc in msg.tool_calls or []:
        fn = tc.get("function", {})
        tokens += estimate_tokens(str(fn.get("name", "")))
        tokens += estimate_tokens(str(fn.get("arguments", "")))
    return tokens


def estimate_messages_tokens(messages: list[ChatMessage]) -> int:
    """Estimate total token count for a message list."""
    return sum(estimate_message_tokens(m) for m in messages)


def _tool_schema_tokens(schema: dict) -> int:
    """Rough token cost of a single tool schema JSON."""
    import json
    raw = json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
    return len(raw) // 4  # ~4 chars per token for JSON


def estimate_request_tokens(
    messages: list[ChatMessage],
    *,
    tools_schemas: list[dict] | None = None,
    system_prompt_tokens: int = 0,
) -> int:
    """Estimate total tokens for the full LLM request.

    = messages tokens + tool schema tokens + system prompt tokens
    """
    total = estimate_messages_tokens(messages) + system_prompt_tokens
    if tools_schemas:
        total += sum(_tool_schema_tokens(s) for s in tools_schemas)
    return total


def resolve_context_budget(config) -> int:
    """Get the effective context budget for the configured model.

    Priority: config.context_compression.context_budget > model lookup > 128K default.
    """
    if hasattr(config, "context_compression") and config.context_compression.context_budget:
        return config.context_compression.context_budget
    model = config.llm.model
    # Strip provider prefix if present (e.g. "deepseek/deepseek-chat")
    for key in _MODEL_CONTEXTS:
        if key in model:
            return _MODEL_CONTEXTS[key]
    return 128_000  # safe default


class TokenBudget:
    """Token budget calculator for context compression decisions.

    Usage::

        budget = TokenBudget(config)
        tokens = budget.estimate(msgs, tools_schemas, system_prompt_tokens)
        if budget.should_trim(tokens):
            msgs = trim_old_tool_results(msgs, protect_count=budget.protect_recent_count)
    """

    def __init__(self, config) -> None:
        cc = getattr(config, "context_compression", None)
        self.enabled = cc.enabled if cc else False
        self.trim_threshold_pct: float = cc.trim_threshold_pct if cc else 0.60
        self.summary_threshold_pct: float = cc.summary_threshold_pct if cc else 0.80
        self.emergency_threshold_pct: float = cc.emergency_threshold_pct if cc else 0.95
        self.protect_recent_count: int = cc.protect_recent_count if cc else 20
        self.protect_recent_tokens: int = cc.protect_recent_tokens if cc else 16000

        self.context_budget = resolve_context_budget(config)
        self._trim_threshold = int(self.context_budget * self.trim_threshold_pct)
        self._summary_threshold = int(self.context_budget * self.summary_threshold_pct)
        self._emergency_threshold = int(self.context_budget * self.emergency_threshold_pct)

    # ---- Public API -------------------------------------------------------

    def estimate(
        self,
        messages: list[ChatMessage],
        *,
        tools_schemas: list[dict] | None = None,
        system_prompt_tokens: int = 0,
    ) -> int:
        return estimate_request_tokens(
            messages,
            tools_schemas=tools_schemas,
            system_prompt_tokens=system_prompt_tokens,
        )

    def should_trim(self, tokens: int) -> bool:
        """True when tokens exceed the trim (Phase 1) threshold."""
        return self.enabled and tokens >= self._trim_threshold

    def should_summarize(self, tokens: int) -> bool:
        """True when tokens exceed the summary (Phase 2) threshold."""
        return self.enabled and tokens >= self._summary_threshold

    def is_emergency(self, tokens: int) -> bool:
        """True when tokens are critically close to the context limit."""
        return self.enabled and tokens >= self._emergency_threshold

    def usage_pct(self, tokens: int) -> float:
        """Return context utilization as a percentage (0.0–1.0+)."""
        return tokens / self.context_budget if self.context_budget else 0.0

    # ---- Error classification --------------------------------------------

    @staticmethod
    def is_context_overflow_error(error_text: str) -> bool:
        """Check whether an error message indicates context overflow.

        Used for reactive compression: when the API returns an overflow error,
        trigger Phase 1 or 2 compression before retrying.
        """
        lowered = error_text.lower()
        return any(
            kw in lowered
            for kw in _OVERFLOW_KEYWORDS
        )


_OVERFLOW_KEYWORDS: list[str] = [
    "maximum context length",
    "context length exceeded",
    "too many tokens",
    "token limit exceeded",
    "prompt is too long",
    "input is too long",
    "exceeds the context window",
    "context window of this model",
    "exceeded the maximum",
]
