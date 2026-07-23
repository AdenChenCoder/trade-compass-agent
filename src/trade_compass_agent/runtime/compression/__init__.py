"""Context compression — progressive context window management.

Three-phase design:

Phase 0: Token budget estimation (budget.py)
Phase 1: Cheap tool-result trimming — no LLM calls (trim.py)
Phase 2: LLM summarization of middle turns (summarizer.py)
Phase 3: Post-compression cleanup (future)

Usage:
    from trade_compass_agent.runtime.compression.budget import TokenBudget
    from trade_compass_agent.runtime.compression.trim import trim_tool_results
    from trade_compass_agent.runtime.compression.summarizer import summarize_middle_turns

    budget = TokenBudget(config)
    tokens = budget.estimate(messages, tools_schemas=schemas)
    if budget.should_summarize(tokens):
        messages, summary, _, _ = summarize_middle_turns(messages, llm_call=...)
"""

from trade_compass_agent.runtime.compression.budget import (
    TokenBudget,
    estimate_messages_tokens,
    estimate_request_tokens,
    estimate_tokens,
    resolve_context_budget,
)
from trade_compass_agent.runtime.compression.summarizer import (
    summarize_middle_turns,
)
from trade_compass_agent.runtime.compression.trim import (
    trim_tool_results,
)

__all__ = [
    "TokenBudget",
    "estimate_messages_tokens",
    "estimate_request_tokens",
    "estimate_tokens",
    "resolve_context_budget",
    "summarize_middle_turns",
    "trim_tool_results",
]
