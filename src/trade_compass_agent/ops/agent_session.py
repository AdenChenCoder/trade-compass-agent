"""ScheduledAgentSession — isolated Agent session for scheduled Jobs.

Each Job gets its own session_id to avoid polluting user conversation history.
Agent failure raises instead of silently degrading.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import replace
from datetime import date
from pathlib import Path

from trade_compass_agent.config import AppConfig
from trade_compass_agent.ops.job_definition import StepContext, StepExecutionError, StepOutput

logger = logging.getLogger(__name__)


SCHEDULER_EXCLUDED_TOOLS = {"schedule_task", "list_scheduled_tasks", "remove_scheduled_task"}

# Prepended to every scheduled-agent prompt — jobs run unattended (no user online).
SCHEDULER_OUTPUT_RULES = (
    "【定时任务输出规范】这是无人值守自动任务，用户不在线。\n"
    "- 只用陈述/总结语气输出结论与建议，禁止向用户提问或请求确认\n"
    "- 禁止「需要你确认」「是否同意」「请告诉我」等反问句式\n"
    "- 不确定项写「待核实：…」或「数据缺口：…」，不要抛给用户\n"
    "- 操作建议用「建议…」「计划…」「关注…」表述\n"
    "- 任何卖出/减仓建议须给出可执行 sell_qty（100股整数倍）；100股持仓禁止「减1/3」「减半仓」\n\n"
)

_SUBSTANTIVE_RESPONSE_MIN_CHARS = 200
SCHEDULED_LLM_TIMEOUT_SECONDS = 180.0


def wrap_scheduler_prompt(prompt: str) -> str:
    """Prepend unattended-job output rules to a scheduler agent prompt."""
    return SCHEDULER_OUTPUT_RULES + prompt


class ScheduledAgentSession:
    """Isolated Agent session for a scheduled Job execution.

    Isolation measures:
    - Unique session_id per job+date (no user history pollution)
    - Recursive scheduling tools excluded (prevent infinite loops)
    - Optional tool whitelist for restricted contexts (e.g. background review)
    """

    def __init__(
        self,
        config: AppConfig,
        *,
        job_id: str,
        run_date: date | None = None,
        step_id: str | None = None,
        tool_whitelist: set[str] | None = None,
        memory_actor: str = "scheduler",
        skill_actor: str | None = None,
    ) -> None:
        self.config = replace(
            config,
            llm=replace(
                config.llm,
                timeout=max(config.llm.timeout, SCHEDULED_LLM_TIMEOUT_SECONDS),
            ),
        )
        self.job_id = job_id
        self._tool_whitelist = tool_whitelist
        self._memory_actor = memory_actor
        self._skill_actor = skill_actor or memory_actor
        d = (run_date or date.today()).isoformat()
        if step_id:
            self.session_id = f"scheduler-{job_id}-{step_id}-{d}"
        else:
            self.session_id = f"scheduler-{job_id}-{d}"

    def run(self, prompt: str, *, timeout: int = 300) -> str:
        """Run an Agent turn. Raises on failure — no silent degradation.

        Compression isolation: each scheduler job uses a distinct persisted
        session. Scheduler sessions never share history or summary state with
        user sessions, and
        SCHEDULER_EXCLUDED_TOOLS prevents recursive scheduling tool calls
        that could bloat context.
        """
        from trade_compass_agent.data.network import run_with_timeout
        from trade_compass_agent.runtime.exceptions import AgentUnavailableError
        from trade_compass_agent.runtime.loop import AgentLoop, TOOL_ROUND_LIMIT_MESSAGE

        def _turn() -> str:
            agent = AgentLoop.from_config(
                self.config,
                memory_actor=self._memory_actor,
                skill_actor=self._skill_actor,
            )
            excluded = set(SCHEDULER_EXCLUDED_TOOLS)
            if self._tool_whitelist is not None:
                all_tools = {
                    s.get("function", s).get("name", "")
                    for s in agent._tools.schemas
                }
                excluded |= all_tools - self._tool_whitelist
            agent._tools._exclude_tools = excluded
            response = agent.run_turn(wrap_scheduler_prompt(prompt), session_id=self.session_id)
            text = _select_scheduler_response_text(
                response.summary,
                self.config.data_dir / "agent_sessions" / f"{self.session_id}.jsonl",
            )
            if not text:
                raise AgentUnavailableError(f"Agent returned empty response for job {self.job_id}")
            if text.startswith(TOOL_ROUND_LIMIT_MESSAGE):
                raise AgentUnavailableError(f"Agent reached tool round limit for job {self.job_id}")
            return text

        return run_with_timeout(_turn, timeout, f"scheduler-agent-{self.job_id}")


async def run_agent_step(
    ctx: StepContext,
    prompt: str,
    job_id: str,
    *,
    step_id: str | None = None,
    tool_whitelist: set[str] | None = None,
) -> StepOutput:
    """Shared Agent step executor. Agent failure = step failure = Job failure."""
    session = ScheduledAgentSession(
        ctx.config,
        job_id=job_id,
        run_date=ctx.date,
        step_id=step_id,
        tool_whitelist=tool_whitelist,
    )
    timeout = ctx.step_timeout_seconds or 300
    try:
        text = await asyncio.to_thread(session.run, prompt, timeout=timeout)
    except Exception as exc:
        raise StepExecutionError(f"Agent 执行失败 ({job_id}): {exc}") from exc
    return StepOutput(message="Agent 分析完成", data={"analysis": text})


def _select_scheduler_response_text(summary: str | None, session_file: Path) -> str:
    """Return the best user-facing text from the latest scheduled agent turn.

    AgentLoop returns only the final assistant message. In scheduled jobs, an
    agent may draft the full report before calling side-effect tools such as
    emit_signal/write_memory, then finish with a short confirmation. The full
    report is still persisted in the session, so recover it for artifacts and
    notifications when it is clearly more substantial than the final summary.
    """
    final_text = (summary or "").strip()
    try:
        lines = session_file.read_text(encoding="utf-8").splitlines()
    except OSError:
        return final_text

    records: list[dict] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(raw, dict) and raw.get("type") != "meta":
            records.append(raw)

    latest_user_idx = -1
    for idx, record in enumerate(records):
        if record.get("role") == "user":
            latest_user_idx = idx

    candidates: list[str] = []
    for record in records[latest_user_idx + 1 :]:
        if record.get("role") != "assistant":
            continue
        content = str(record.get("content") or "").strip()
        if len(content) >= _SUBSTANTIVE_RESPONSE_MIN_CHARS:
            candidates.append(content)

    if not candidates:
        return final_text

    best = max(candidates, key=len)
    if len(best) > max(len(final_text) * 2, len(final_text) + _SUBSTANTIVE_RESPONSE_MIN_CHARS):
        return best
    return final_text
