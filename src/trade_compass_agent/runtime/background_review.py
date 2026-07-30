"""Post-turn background self-improvement review.

After each agent turn, if nudge thresholds are reached, a daemon thread
spawns a minimal review agent with only write_knowledge + skill_manage tools.
Writes land on disk immediately but don't affect the current session's
system prompt (frozen snapshot pattern).
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable

logger = logging.getLogger(__name__)

MEMORY_NUDGE_INTERVAL = 10  # user turns
SKILL_NUDGE_INTERVAL = 15   # tool iterations without skill write

MEMORY_REVIEW_PROMPT = """\
回顾上述对话，考虑是否需要更新 memory:

关注点:
1. 用户的风险偏好或投资风格有变化吗？
2. 发现了新的市场规律或数据源限制吗？
3. 当前使用的策略/工具有什么需要记住的特性？

只把「声明性记忆」写入 write_knowledge(action=add)：长期判断原则、事实、用户偏好，最好是一句话。
不要把触发条件、执行步骤、工具调用顺序、评分表、阈值表、输出模板或 load_skill 路由写入 memory；
这些都属于 skill_manage(create/patch/edit)。
**注意**：Agent add 为低信任暂存（confidence=0.4），默认不会注入后续 prompt；
只有经 promotion 晋升或用户 pin 的条目才会成为高信任记忆。
若用户明确要求「记住/固定」某条，只能提示用户在前台确认 pin；后台不得代为 pin。

如果没有值得保存的，回复 'Nothing to save.'

## 绝不保存:
- 临时数据查询结果（行情、价格）
- 一次性任务叙述
- 工具暂时故障（保存重试方案而非故障本身）
- 环境特定的配置问题
"""

SKILL_REVIEW_PROMPT = """\
回顾上述交易分析过程，考虑是否需要创建或更新 skill:

关注点:
1. 本次分析中是否发现了可复用的交易模式？
2. 现有 skill 是否有过时的参数或流程需要更新？
3. 如果使用了某个 skill 发现它不准确，立即 patch。
4. 凡是内容包含触发条件、执行步骤、工具调用顺序、评分/阈值表、输出模板或 load_skill 路由，归入 skill，不写 knowledge。

优先级:
1. patch 已加载的 skill（比创建新的好）
2. 创建类级别的 umbrella skill（不是一次性任务记录）
3. 添加 references/ 支持文件

## 绝不保存:
- 一次性查询（"查下贵州茅台行情"）
- 工具暂时不可用的状态 → 保存重试模式而非故障
- 具体价格/日期数据点
- 否定式断言（"XX API 不能用"）

Skill 格式要求:
- 必须以 YAML frontmatter 开始 (---\\nname: ...\\ndescription: ...\\ncategory: ...\\n---)
- category 可选: screening, risk, analysis, execution, macro, general
- 包含: 触发条件、执行流程、参数、历史表现（如有）
"""

COMBINED_REVIEW_PROMPT = f"""\
你是 Trade Compass 的后台自省代理。只能使用 write_knowledge 和 skill_manage 两个工具。

---

{MEMORY_REVIEW_PROMPT}

---

{SKILL_REVIEW_PROMPT}

---

如果两方面都没有值得保存的内容，直接回复 'Nothing to save.' 并停止。
"""


class ReviewNudgeTracker:
    """Tracks nudge counters for background review trigger decisions."""

    def __init__(
        self,
        memory_interval: int = MEMORY_NUDGE_INTERVAL,
        skill_interval: int = SKILL_NUDGE_INTERVAL,
    ):
        self._memory_interval = memory_interval
        self._skill_interval = skill_interval
        self._turns_since_memory = 0
        self._iters_since_skill = 0
        self._memory_written_this_turn = False
        self._lock = threading.Lock()

    def on_user_turn(self) -> None:
        with self._lock:
            self._turns_since_memory += 1

    def on_tool_iteration(self) -> None:
        with self._lock:
            self._iters_since_skill += 1

    def on_memory_write(self) -> None:
        with self._lock:
            self._turns_since_memory = 0
            self._memory_written_this_turn = True

    def on_skill_write(self) -> None:
        with self._lock:
            self._iters_since_skill = 0

    def should_review(self) -> tuple[bool, bool]:
        """Returns (should_review_memory, should_review_skills).

        Implements dual-write exclusion: if main agent wrote memory this turn,
        skip memory review even if the nudge threshold was met.
        """
        with self._lock:
            mem = self._turns_since_memory >= self._memory_interval
            if self._memory_written_this_turn:
                mem = False
            skill = self._iters_since_skill >= self._skill_interval
            return mem, skill

    def reset_turn_flags(self) -> None:
        """Reset per-turn flags. Call at the start of each user turn."""
        with self._lock:
            self._memory_written_this_turn = False

    def reset_after_review(self, reviewed_memory: bool, reviewed_skills: bool) -> None:
        with self._lock:
            if reviewed_memory:
                self._turns_since_memory = 0
            if reviewed_skills:
                self._iters_since_skill = 0


REVIEW_TOOL_WHITELIST = {"write_knowledge", "skill_manage", "search_memory", "session_search"}


def spawn_background_review(
    messages_snapshot: list[dict[str, Any]],
    review_memory: bool,
    review_skills: bool,
    llm_call: Callable[[str, list[dict[str, Any]]], str],
    memory_write: Callable[[str, str, str], dict[str, Any]],
    skill_manage: Callable[[str, dict[str, Any]], dict[str, Any]],
    *,
    memory_store=None,
    config=None,
) -> None:
    """Spawn a daemon thread for background self-improvement.

    Runs a full agent loop (ScheduledAgentSession) with
    tool-use restricted to memory/skill tools. Falls back to single
    llm_call if config is unavailable.
    """
    thread = threading.Thread(
        target=_run_review,
        args=(messages_snapshot, review_memory, review_skills, llm_call, memory_write, skill_manage, memory_store, config),
        daemon=True,
        name="bg-review",
    )
    thread.start()
    logger.info("Background review spawned (memory=%s, skills=%s)", review_memory, review_skills)


def _run_review(
    messages: list[dict[str, Any]],
    review_memory: bool,
    review_skills: bool,
    llm_call: Callable,
    memory_write: Callable,
    skill_manage: Callable,
    memory_store=None,
    config=None,
) -> None:
    """Run the review in background thread. Errors are logged, never raised."""
    try:
        if memory_store and review_memory:
            _reinforce_relevant_entries(messages, memory_store)

        if review_memory and review_skills:
            prompt = COMBINED_REVIEW_PROMPT
        elif review_memory:
            prompt = MEMORY_REVIEW_PROMPT
        else:
            prompt = SKILL_REVIEW_PROMPT

        if config is not None:
            _run_review_agent_loop(config, prompt)
        else:
            _run_review_text_only(llm_call, messages, prompt)

        if memory_store:
            archived = memory_store.archive_stale("memory")
            if archived:
                logger.info("Archived %d stale memory entries", len(archived))
    except Exception as exc:
        logger.warning("Background review failed: %s", exc, exc_info=True)


def _run_review_agent_loop(config, prompt: str) -> None:
    """Run review via ScheduledAgentSession — full agent loop with tool calls.

    Fork a full agent with its tool whitelist restricted to
    write_knowledge + skill_manage + search tools.
    """
    from trade_compass_agent.ops.agent_session import ScheduledAgentSession

    session = ScheduledAgentSession(config, job_id="background-review", tool_whitelist=REVIEW_TOOL_WHITELIST)
    try:
        text = session.run(prompt, timeout=120)
        if text and "nothing to save" not in text.lower():
            logger.info("Background review (agent loop) completed (len=%d)", len(text))
        else:
            logger.debug("Background review: nothing to save")
    except Exception as exc:
        logger.warning("Background review agent loop failed: %s", exc)


def _run_review_text_only(llm_call: Callable, messages: list[dict[str, Any]], prompt: str) -> None:
    """Fallback: single LLM call without tool dispatch (legacy mode)."""
    clean_msgs = [
        m for m in messages
        if m.get("role") != "tool"
        and not (m.get("role") == "assistant" and not m.get("content"))
    ]
    review_messages = list(clean_msgs) + [{"role": "user", "content": prompt}]
    response = llm_call(
        "你是 Trade Compass 后台自省代理。你只能调用 write_knowledge 和 skill_manage 工具。",
        review_messages,
    )
    if response and "nothing to save" not in response.lower():
        logger.info("Background review completed (len=%d)", len(response))
    else:
        logger.debug("Background review: nothing to save")


def _reinforce_relevant_entries(messages: list[dict[str, Any]], memory_store) -> None:
    """Reinforce memory entries that appear in the current conversation context."""
    try:
        entries = memory_store.list_active("memory", min_confidence=0.0)
        if not entries:
            return
        conv_text = " ".join(
            m.get("content", "")[:500] for m in messages[-10:]
        ).lower()
        for entry in entries:
            text = entry.text.strip()
            if len(text) < 4:
                continue
            # For CJK text (no spaces), check if a prefix substring matches
            # For spaced text, check first 5 words
            words = text.split()
            if len(words) >= 3:
                key_phrase = " ".join(words[:5]).lower()
            else:
                # CJK: use first 8 characters as key phrase
                key_phrase = text[:8].lower()
            if key_phrase in conv_text:
                memory_store.reinforce(text[:50], "memory")
    except Exception as exc:
        logger.debug("Reinforce scan failed: %s", exc)
