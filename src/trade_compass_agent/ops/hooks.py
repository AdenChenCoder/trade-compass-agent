"""Hook system for Job lifecycle events.

Hooks fire at specific phases of Job execution. Built-in hooks handle
common cross-cutting concerns; user hooks can be registered for custom logic.

Phases:
- pre_job: before any step runs (can skip the job)
- post_job: after all steps complete (success or failure)
- pre_step: before each step
- post_step: after each step
- on_failure: when a step or job fails
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable

from trade_compass_agent.ops.job_definition import JobDefinition, StepContext
from trade_compass_agent.ops.run_store import RunRecord

logger = logging.getLogger(__name__)

Phase = str  # "pre_job" | "post_job" | "pre_step" | "post_step" | "on_failure"


@dataclass
class HookContext:
    job: JobDefinition
    run: RunRecord
    phase: Phase
    step_id: str | None = None
    error: str | None = None
    ctx: StepContext | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


HookHandler = Callable[[HookContext], None]


@dataclass
class HookRegistration:
    name: str
    phase: Phase
    handler: HookHandler
    priority: int = 100  # lower = runs first


class HookRegistry:
    """Registry for lifecycle hooks."""

    def __init__(self) -> None:
        self._hooks: dict[Phase, list[HookRegistration]] = defaultdict(list)

    def register(self, name: str, phase: Phase, handler: HookHandler, *, priority: int = 100) -> None:
        self._hooks[phase].append(HookRegistration(name=name, phase=phase, handler=handler, priority=priority))
        self._hooks[phase].sort(key=lambda h: h.priority)
        logger.debug("Hook registered: %s @ %s (priority %d)", name, phase, priority)

    def fire(self, context: HookContext) -> list[str]:
        """Fire all hooks for the given phase. Returns list of hook names that ran.

        SkipJobError is re-raised (not swallowed) so the executor can skip the job.
        """
        results = []
        for hook in self._hooks.get(context.phase, []):
            try:
                hook.handler(context)
                results.append(hook.name)
            except SkipJobError:
                raise
            except Exception as exc:
                logger.warning("Hook %s failed: %s", hook.name, exc)
                results.append(f"{hook.name}(ERROR)")
        return results

    def phases(self) -> list[Phase]:
        return list(self._hooks.keys())

    def hooks_for(self, phase: Phase) -> list[str]:
        return [h.name for h in self._hooks.get(phase, [])]


# ---------------------------------------------------------------------------
# Built-in hooks
# ---------------------------------------------------------------------------

def hook_trading_day_guard(ctx: HookContext) -> None:
    """Skip job on non-trading days (for trading_day_only jobs)."""
    if ctx.job.trading_day_only:
        from trade_compass_agent.ops.trading_calendar import is_trading_day
        if not is_trading_day():
            raise SkipJobError("非交易日")


def hook_memory_reflection(ctx: HookContext) -> None:
    """Write run result to memory vault as reflection.

    Extracts substantive agent analysis from step data rather than
    storing bare statistics. Skips writing if no meaningful content found.
    """
    if ctx.run.status not in ("completed", "failed"):
        return
    if ctx.ctx is None:
        return

    from trade_compass_agent.config import settings_from_config
    from trade_compass_agent.memory import MemoryVault

    settings = settings_from_config(ctx.ctx.config)
    vault = MemoryVault(settings.memory_dir)
    path = vault.root / "reflections" / f"{ctx.run.job_id}-{ctx.ctx.date.isoformat()}.md"
    path.parent.mkdir(parents=True, exist_ok=True)

    lines = [
        f"# {ctx.job.name} — {ctx.ctx.date.isoformat()}",
        f"Status: {ctx.run.status}",
    ]
    if ctx.run.error:
        lines.append(f"Error: {ctx.run.error}")

    rich_parts = _extract_rich_content(ctx)
    if rich_parts:
        lines.append("")
        lines.extend(rich_parts)
    else:
        lines.append(f"Summary: {ctx.run.message}")

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _extract_rich_content(ctx: HookContext) -> list[str]:
    """Extract agent analysis text from step upstream data."""

    parts: list[str] = []
    if ctx.ctx is None:
        return parts
    for step_id, output in ctx.ctx.upstream.items():
        analysis = output.data.get("analysis") or output.data.get("text") or output.data.get("content")
        if isinstance(analysis, str) and len(analysis) > 50:
            parts.append(f"## {step_id}")
            parts.append("")
            parts.append(analysis)
            parts.append("")
    return parts


def hook_channel_alert_on_failure(ctx: HookContext) -> None:
    """Push alert to external channels when a job fails."""
    if ctx.run.status != "failed":
        return
    from trade_compass_agent.ops.delivery import DeliveryRouter
    if ctx.ctx:
        router = DeliveryRouter(ctx.ctx.config)
        router.push_immediate(
            f"定时任务失败: {ctx.job.name}",
            ctx.run.error or ctx.run.message or "未知错误",
            severity="critical",
        )


class SkipJobError(Exception):
    """Raised by pre_job hooks to skip the job."""


def create_default_registry() -> HookRegistry:
    """Create a HookRegistry with all built-in hooks registered."""
    registry = HookRegistry()
    registry.register("memory_reflection", "post_job", hook_memory_reflection, priority=50)
    registry.register("channel_alert_on_failure", "on_failure", hook_channel_alert_on_failure, priority=50)
    return registry
