"""ScheduledAgentSession — isolated Agent session for scheduled Jobs.

Each Job gets its own session_id to avoid polluting user conversation history.
Agent failure raises instead of silently degrading.
"""

from __future__ import annotations

import asyncio
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
    "- 榜单只代表返回范围，未入榜不等于退出领涨、退潮或资金流出；跨时点比较须确认日期、来源及分类口径可比，"
    "否则只报告本次快照及比较缺口，不得写成已发生的变化；假设须明确标为假设。该约束同时适用于正文和写入记忆的内容\n"
    "- 报告涉及当前持仓、成交或行情判断，须先以本轮工具核对相关事实；历史对话只能作线索，不能冒充本轮取数。"
    "仅更正文稿且未重新取数时，限定为更正说明，不夹带当前操作建议\n"
    "- 工具没有返回的日期、数值、单位或事件细节不能补写为确定事实；公告标题不证明分红日期或金额。"
    "新闻按原始事件日期标注，旧新闻不能写成今日行情；数值换算须保留原始单位并核算。"
    "任务失败只能证明相应检查或报告未完成，不能据此断言没有任何成交或风控执行\n"
    "- 最终答复必须是一份完整报告，先写最终结论，再写依据、执行结果与数据缺口；工具调用前的中间稿仅保留在过程记录中\n"
    "- 完成工具调用后，将仍有效的分析和后续更正整合进最终答复，删除已撤回的结论，不要仅回复「已记录」「已完成」\n"
    "- 遵守本轮模拟交易权限：允许自主交易且作出买卖决策时，调用交易工具执行，不停留在建议或等待用户确认\n"
    "- 未执行的建议才用「建议…」「计划…」「关注…」；已成交须依据工具回执，失败与继续持有须说明原因\n"
    "- 任何卖出/减仓建议须给出可执行 sell_qty（100股整数倍）；100股持仓禁止「减1/3」「减半仓」\n\n"
)

SCHEDULED_LLM_TIMEOUT_SECONDS = 900.0


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
        run_id: str = "",
        attempt: int = 1,
        initial_portfolio: dict | None = None,
    ) -> None:
        self.config = replace(
            config,
            llm=replace(
                config.llm,
                timeout=max(config.llm.timeout, SCHEDULED_LLM_TIMEOUT_SECONDS),
                max_retries=0,
            ),
        )
        self.job_id = job_id
        self._tool_whitelist = tool_whitelist
        self._memory_actor = memory_actor
        self._skill_actor = skill_actor or memory_actor
        self.run_id = run_id
        self.step_id = step_id
        self.initial_portfolio = initial_portfolio
        self.attempt = max(1, int(attempt))
        self.warnings: list[str] = []
        self.degraded = False
        self._verify_report_grounding = job_id in {"close", "eod_review", "postmarket", "weekly"} or (
            job_id == "premarket" and str(step_id or "").startswith("agent_briefing")
        )
        self.run_date = run_date or date.today()
        d = self.run_date.isoformat()
        if step_id:
            self.session_id = f"scheduler-{job_id}-{step_id}-{d}"
        else:
            self.session_id = f"scheduler-{job_id}-{d}"

    def run(self, prompt: str, *, timeout: int = 1800) -> str:
        """Run an Agent turn. Raises on failure — no silent degradation.

        Compression isolation: each scheduler job uses a distinct persisted
        session. Scheduler sessions never share history or summary state with
        user sessions, and
        SCHEDULER_EXCLUDED_TOOLS prevents recursive scheduling tool calls
        that could bloat context.
        """
        from trade_compass_agent.runtime.exceptions import AgentUnavailableError
        from trade_compass_agent.data.network import drain_timeout_workers, run_with_timeout
        from trade_compass_agent.runtime.loop import AgentLoop, TOOL_ROUND_LIMIT_MESSAGE
        from trade_compass_agent.ops.autonomous_trading import JOB_ID
        from trade_compass_agent.llm.providers import _request_cancelled, _request_deadline
        import threading
        import time

        stopped = threading.Event()
        deadline = time.monotonic() + timeout
        # Commit guards may run on a tool thread without these ContextVars.
        parent_deadline = _request_deadline.get()
        parent_cancelled = _request_cancelled.get()
        if parent_deadline is not None:
            deadline = min(deadline, parent_deadline)

        def cancelled() -> bool:
            from trade_compass_agent.portfolio.trading_policy import AutonomousTradingStore
            return (stopped.is_set() or time.monotonic() >= deadline
                    or bool(parent_cancelled and parent_cancelled())
                    or (self.job_id == JOB_ID and not AutonomousTradingStore(self.config.data_dir).read()))

        def guard_execution() -> None:
            from trade_compass_agent.portfolio.trading_policy import TradeRejected
            if cancelled():
                raise TradeRejected("本轮自主交易已停止或超时，本次未成交", "execution_inactive")

        def _turn() -> str:
            from trade_compass_agent.llm.usage import llm_usage_context
            from trade_compass_agent.llm.providers import llm_request_deadline

            with drain_timeout_workers(), llm_request_deadline(deadline, is_cancelled=cancelled), llm_usage_context(
                job_id=self.job_id,
                run_id=self.run_id,
                session_id=self.session_id,
                attempt=self.attempt,
            ):
                agent = AgentLoop.from_config(
                    self.config,
                    memory_actor=self._memory_actor,
                    skill_actor=self._skill_actor,
                )
                agent.report_date = self.run_date.isoformat()
                agent.verify_report_grounding = self._verify_report_grounding
                agent.initial_portfolio = self.initial_portfolio
                if self.job_id == "morning_plan" and self.step_id == "agent_plan":
                    from trade_compass_agent.portfolio.planning import premarket_reference_date
                    reference_date = premarket_reference_date(self.run_date)
                    agent.planning_reference_date = reference_date.isoformat() if reference_date else None
                    agent._tools.planning_reference_date = reference_date
                excluded = set(SCHEDULER_EXCLUDED_TOOLS)
                if self._tool_whitelist is not None:
                    all_tools = {
                        s.get("function", s).get("name", "")
                        for s in agent._tools.schemas
                    }
                    excluded |= all_tools - self._tool_whitelist
                agent._tools._exclude_tools = excluded
                agent._tools.trade_execution_guard = guard_execution
                for store_name in ("_memory_store", "_skill_store"):
                    store = getattr(agent, store_name, None)
                    if store is not None:
                        store._commit_guard = guard_execution
                turn_options = {"is_cancelled": cancelled}
                dated_prompt = (f"本次任务日期：{self.run_date.isoformat()}（{self.config.scheduler.timezone}）。"
                                "本任务的今天及本周均以此日期为基准，工具取回时间不改变归档日。\n\n" + prompt)
                response = agent.run_turn(wrap_scheduler_prompt(dated_prompt), session_id=self.session_id,
                                          **turn_options)
                self.warnings = list(getattr(response, "warnings", []) or [])
                self.degraded = getattr(response, "degraded", False) is True
                if getattr(response, "interrupted", False) is True:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f"Scheduled agent {self.job_id} deadline reached")
                    raise AgentUnavailableError(f"Agent interrupted for job {self.job_id}")
                text = _select_scheduler_response_text(
                    response.summary,
                    self.config.data_dir / "agent_sessions" / f"{self.session_id}.jsonl",
                )
                if not text:
                    raise AgentUnavailableError(f"Agent returned empty response for job {self.job_id}")
                if text.startswith(TOOL_ROUND_LIMIT_MESSAGE):
                    raise AgentUnavailableError(f"Agent reached tool round limit for job {self.job_id}")
                return text

        try:
            try:
                with drain_timeout_workers():
                    result = run_with_timeout(
                        _turn, max(.001, deadline - time.monotonic()),
                        f"Scheduled agent {self.job_id} deadline", is_cancelled=cancelled,
                        on_timeout=stopped.set,
                    )
            except Exception as exc:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"Scheduled agent {self.job_id} deadline reached") from exc
                raise
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Scheduled agent {self.job_id} deadline reached")
            return result
        finally:
            stopped.set()


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
        run_id=ctx.run_id,
        initial_portfolio=(ctx.upstream.get("portfolio_check", StepOutput.empty()).data
                           if job_id == "morning_plan" and step_id == "agent_plan" else None),
    )
    timeout = ctx.step_timeout_seconds or 1800
    try:
        text = await asyncio.to_thread(session.run, prompt, timeout=timeout)
    except Exception as exc:
        raise StepExecutionError(f"Agent 执行失败 ({job_id}): {exc}") from exc
    return StepOutput(message="Agent 分析完成", data={"analysis": text,
        "warnings": session.warnings, "degraded": session.degraded, "session_id": session.session_id})


def _select_scheduler_response_text(summary: str | None, session_file: Path) -> str:
    """Publish the final reply; intermediate drafts remain in session history.

    Length cannot establish whether a draft is still valid after tool results
    or corrections. AgentLoop already attaches evidence to the final reply.
    """
    return (summary or "").strip()
