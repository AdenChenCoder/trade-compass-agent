"""Declarative Job definitions, operation context, and registry.

Each Job declares schedule, delivery, and the workflow asset it triggers.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from trade_compass_agent.config import AppConfig

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RetryPolicy:
    max_retries: int = 0
    backoff_seconds: int = 60
    retry_on_timeout: bool = False


@dataclass(frozen=True)
class DeliveryConfig:
    channels: tuple[str, ...] = ("web_log",)
    silent_on_success: bool = False


@dataclass(frozen=True)
class JobDefinition:
    """Complete Job declaration."""
    id: str
    name: str
    description: str
    schedule: str  # "trading_day HH:MM" | "sat HH:MM" | cron
    workflow_id: str | None = None
    workflow_inputs: dict[str, Any] = field(default_factory=dict)
    enabled: bool = True
    trading_day_only: bool = True
    timeout_seconds: int = 600
    retry_policy: RetryPolicy = field(default_factory=RetryPolicy)
    delivery: DeliveryConfig = field(default_factory=DeliveryConfig)
    agent_session: str | None = None
    tags: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Step context / output
# ---------------------------------------------------------------------------

class StepExecutionError(Exception):
    """Raised when a step fails. Propagates to fail the entire Job."""


@dataclass
class StepOutput:
    message: str
    data: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def empty(cls) -> StepOutput:
        return cls(message="", data={})


@dataclass
class StepContext:
    config: AppConfig
    date: date
    upstream: dict[str, StepOutput] = field(default_factory=dict)
    reflection_context: str | None = None
    step_timeout_seconds: int | None = None
    job_id: str = ""
    run_id: str = ""


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

class JobRegistry:
    """Job definition registry. Built-in 6 + extensible."""

    def __init__(self) -> None:
        self._jobs: dict[str, JobDefinition] = {}

    def register(self, job: JobDefinition) -> None:
        self._jobs[job.id] = job
        logger.debug("Registered job: %s", job.id)

    def get(self, job_id: str) -> JobDefinition | None:
        return self._jobs.get(job_id)

    def all(self) -> list[JobDefinition]:
        return list(self._jobs.values())

    def ids(self) -> list[str]:
        return list(self._jobs.keys())

    def from_config(self, config: AppConfig) -> None:
        """Register the 6 built-in Jobs from config schedules."""
        from trade_compass_agent.ops.builtin_job_delivery import BuiltinJobDeliveryStore

        s = config.scheduler
        overrides = BuiltinJobDeliveryStore(config.data_dir / "scheduler.db").all()
        builtin = _builtin_jobs(s, overrides)
        for job in builtin:
            self.register(job)


def _delivery(
    job_id: str,
    default: tuple[str, ...],
    overrides: dict[str, tuple[str, ...]],
    *,
    silent_on_success: bool = False,
) -> DeliveryConfig:
    channels = overrides.get(job_id, default)
    return DeliveryConfig(channels=channels, silent_on_success=silent_on_success)


def _builtin_jobs(s, overrides: dict[str, tuple[str, ...]] | None = None) -> list[JobDefinition]:
    """Construct the 6 built-in JobDefinitions."""
    o = overrides or {}
    return [
        JobDefinition(
            id="premarket",
            name="盘前扫描",
            description="持仓出场信号 + 隔夜新闻 + 全球市场 → 盘前操作建议",
            schedule=f"trading_day {s.premarket_time}",
            workflow_id="premarket_briefing",
            timeout_seconds=1800,
            agent_session="scheduler-premarket-{date}",
            delivery=_delivery("premarket", ("web_log", "feishu", "wecom", "weixin"), o),
        ),
        JobDefinition(
            id="morning_plan",
            name="晨间计划",
            description="L1-L4 选股 → L5 AI 审判 → 板块资金流 → 交易计划",
            schedule=f"trading_day {s.morning_plan_time}",
            timeout_seconds=1800,
            workflow_id="morning_plan",
            agent_session="scheduler-morning-{date}",
            delivery=_delivery("morning_plan", ("web_log", "feishu", "wecom", "weixin"), o),
        ),
        JobDefinition(
            id="close",
            name="收盘检查",
            description="Mark-to-market + 出场信号 → 持仓逻辑分析",
            schedule=f"trading_day {s.close_time}",
            workflow_id="close_check",
            timeout_seconds=1200,
            agent_session="scheduler-close-{date}",
            delivery=_delivery("close", ("web_log", "feishu", "wecom", "weixin"), o),
        ),
        JobDefinition(
            id="eod_review",
            name="盘后复盘",
            description="P&L 复盘 + 信号追踪 + 决策质量评估",
            schedule=f"trading_day {s.eod_review_time}",
            workflow_id="eod_review",
            timeout_seconds=1800,
            agent_session="scheduler-eod-{date}",
            delivery=_delivery("eod_review", ("web_log", "feishu", "wecom", "weixin"), o),
        ),
        JobDefinition(
            id="postmarket",
            name="盘后归档",
            description="审计摘要 + 记忆压缩 + 反思 + 交易日记 + Dreaming",
            schedule=f"trading_day {s.postmarket_time}",
            workflow_id="postmarket_archive",
            agent_session="scheduler-postmarket-{date}",
            delivery=_delivery("postmarket", ("web_log",), o, silent_on_success=True),
        ),
        JobDefinition(
            id="weekly",
            name="周度回顾",
            description="周度摘要 + 策略回顾 + 周度 Dreaming",
            schedule=f"{s.weekly_day} {s.weekly_time}",
            trading_day_only=False,
            workflow_id="weekend_review",
            agent_session="scheduler-weekly-{date}",
            delivery=_delivery("weekly", ("web_log", "feishu", "wecom", "weixin"), o),
        ),
    ]
