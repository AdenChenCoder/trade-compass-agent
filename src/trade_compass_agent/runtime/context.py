from __future__ import annotations

import logging

from trade_compass_agent.llm.providers import ChatMessage
from trade_compass_agent.runtime.bootstrap import build_session_state, build_stable_system_prompt
from trade_compass_agent.runtime.compression.trim import trim_tool_results
from trade_compass_agent.runtime.skills import AgentSkillsConfig, SkillInfo

SESSION_STATE_NAME = "session_state"

logger = logging.getLogger(__name__)

# Token budget: approximate char-to-token ratio for Chinese text
_CHARS_PER_TOKEN = 1.5
_SYSTEM_PROMPT_TOKEN_BUDGET = 4000
_MEMORY_TOKEN_BUDGET = 900  # USER ~300 + KNOWLEDGE ~600
_SKILLS_TOKEN_BUDGET = 400


def _sanitize_tool_calls(history: list[ChatMessage]) -> list[ChatMessage]:
    """Ensure every assistant tool_calls message has matching tool responses.

    If a previous turn crashed mid-execution, or parallel workflow operations
    interleaved assistant/tool messages in one session, the history may
    contain assistant tool_calls without adjacent tool responses. LLM APIs
    reject that with a 400 error. We patch by locating matching tool
    messages anywhere before the next user/assistant turn and inserting
    synthetic responses for any still-missing tool_call_ids.
    """
    used_tool_indices: set[int] = set()
    result: list[ChatMessage] = []
    i = 0
    while i < len(history):
        msg = history[i]
        if msg.role == "assistant" and msg.tool_calls:
            result.append(msg)
            expected_ids = [tc["id"] for tc in msg.tool_calls if "id" in tc]
            expected_set = set(expected_ids)
            found: dict[str, ChatMessage] = {}
            for j in range(i + 1, len(history)):
                if j in used_tool_indices:
                    continue
                nxt = history[j]
                if nxt.role == "user":
                    break
                if nxt.role == "assistant" and nxt.tool_calls:
                    break
                if nxt.role == "tool" and nxt.tool_call_id in expected_set:
                    found[nxt.tool_call_id] = nxt
                    used_tool_indices.add(j)
            for tc_id in expected_ids:
                if tc_id in found:
                    result.append(found[tc_id])
                else:
                    logger.warning("Patching orphan tool_call_id %s in history", tc_id)
                    result.append(ChatMessage(
                        role="tool",
                        content="[工具调用未完成 — 上次执行中断]",
                        tool_call_id=tc_id,
                    ))
            i += 1
        elif msg.role == "tool" and i in used_tool_indices:
            i += 1
        elif msg.role == "tool":
            logger.warning("Skipping orphan tool message without matching assistant turn")
            i += 1
        else:
            result.append(msg)
            i += 1
    return result


def _estimate_tokens(text: str) -> int:
    """Rough token estimate for mixed Chinese/English text."""
    return int(len(text) / _CHARS_PER_TOKEN)


class ContextBuilder:
    """Builds a session-stable instruction prefix and append-only session state.

    The system message and tool list are snapshotted for the session. Memory,
    user rules, the skills catalog and the trading flag are later messages, so
    a change extends the cached prompt instead of rewriting its beginning.
    """

    def __init__(
        self,
        *,
        memory_dir,
        skills: list[SkillInfo],
        skills_config: AgentSkillsConfig | None = None,
        memory_store=None,
        rules_enabled: bool = True,
        rules_char_limit: int = 4000,
        compression_config=None,
        trading_policy: str = "",
        frozen_system: str | None = None,
    ) -> None:
        self.memory_dir = memory_dir
        self.skills = skills
        self._skills_config = skills_config
        self._memory_store = memory_store
        self._rules_enabled = rules_enabled
        self._rules_char_limit = rules_char_limit
        self._trading_policy = trading_policy
        self._compression_config = compression_config
        self._system = frozen_system if frozen_system is not None else build_stable_system_prompt()
        self._system_tokens = _estimate_tokens(self._system)

    @property
    def system_prompt(self) -> str:
        """Frozen system prompt — never changes within a session."""
        return self._system

    @property
    def system_prompt_tokens(self) -> int:
        return self._system_tokens

    def current_session_state(self) -> str:
        return build_session_state(
            memory_dir=self.memory_dir,
            skills=self.skills,
            skills_config=self._skills_config,
            memory_store=self._memory_store,
            rules_enabled=self._rules_enabled,
            rules_char_limit=self._rules_char_limit,
            trading_policy=self._trading_policy,
        )

    def build_messages(
        self,
        history: list[ChatMessage],
        user_message: str,
        *,
        session_state: str = "",
    ) -> list[ChatMessage]:
        messages = [ChatMessage(role="system", content=self._system)]
        sanitized = _sanitize_tool_calls(history)
        # Phase 1: smart tool-result trimming when compression is enabled
        if self._compression_config and self._compression_config.enabled:
            trimmed, pruned, _ = trim_tool_results(
                sanitized,
                protect_recent_count=self._compression_config.protect_recent_count,
                protect_recent_tokens=self._compression_config.protect_recent_tokens,
            )
            if pruned > 0:
                logger.debug("trim_tool_results: pruned %d messages", pruned)
            messages.extend(trimmed)
        else:
            messages.extend(sanitized)
        if session_state and _latest_session_state(messages) != session_state:
            messages.append(ChatMessage(
                role="user", content=session_state, name=SESSION_STATE_NAME,
            ))
        messages.append(ChatMessage(role="user", content=user_message))
        return messages


def _latest_session_state(messages: list[ChatMessage]) -> str | None:
    for message in reversed(messages):
        if message.name == SESSION_STATE_NAME:
            return message.content or ""
    return None
