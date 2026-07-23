"""JobExecutor — scheduler binding executor.

Built-in scheduler jobs bind to workflow assets. The workflow runtime owns the
business step DAG; the scheduler only starts the requested workflow and records
the run.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import date
from typing import Any

from trade_compass_agent.config import AppConfig
from trade_compass_agent.ops.job_definition import (
    JobDefinition,
    StepContext,
    StepOutput,
)
from trade_compass_agent.ops.hooks import HookContext, HookRegistry, SkipJobError
from trade_compass_agent.ops.reflection import JobReflection
from trade_compass_agent.ops.run_content import extract_analysis_from_workflow_output, workflow_run_message
from trade_compass_agent.ops.run_store import RunRecord, SqliteRunStore

logger = logging.getLogger(__name__)


class JobExecutor:
    """Execute a JobDefinition by triggering its bound workflow."""

    def __init__(
        self,
        config: AppConfig,
        run_store: SqliteRunStore,
        *,
        hook_registry: HookRegistry | None = None,
        reflection: JobReflection | None = None,
    ) -> None:
        self.config = config
        self.run_store = run_store
        self.hooks = hook_registry or HookRegistry()
        self.reflection = reflection or JobReflection(config.memory_dir)

    async def execute(
        self,
        job: JobDefinition,
        *,
        trigger: str = "scheduler",
        reflection_context: str | None = None,
    ) -> RunRecord:
        run = self.run_store.create_run(job.id, trigger=trigger)

        if trigger not in {"api", "cli"} and job.trading_day_only and not _is_trading_day():
            self.run_store.skip_run(run, reason="非交易日")
            return run

        if self.run_store.is_job_running(job.id):
            self.run_store.skip_run(run, reason="同一 Job 正在运行（overlap guard）")
            return run

        self.run_store.start_run(run)

        # Resolve stale pending reflections, then inject past lessons
        if job.agent_session:
            from trade_compass_agent.memory.memory_store import MemoryStore
            from trade_compass_agent.ops.reflection_resolver import make_market_resolve_fn

            mem_store = MemoryStore(
                self.config.memory_dir,
                min_inject_confidence=self.config.memory.governance.min_inject_confidence,
            )
            self.reflection.resolve_pending(
                job.id,
                resolve_fn=make_market_resolve_fn(self.config),
                mem_store=mem_store,
                config=self.config,
            )
            if reflection_context is None:
                reflection_context = self.reflection.get_context(
                    job.id,
                    limit=5,
                    sanitize=self.config.memory.recall.sanitize_reflections,
                ) or None

        ctx = StepContext(
            config=self.config,
            date=date.today(),
            reflection_context=reflection_context,
            job_id=job.id,
            run_id=run.id,
        )

        hook_ctx = HookContext(job=job, run=run, phase="pre_job", ctx=ctx)
        try:
            self.hooks.fire(hook_ctx)
        except SkipJobError as exc:
            self.run_store.skip_run(run, reason=str(exc))
            return run

        if not job.workflow_id:
            error = f"Job {job.id} has no workflow_id"
            self.run_store.fail_run(run, error=error, message=error)
            self.hooks.fire(HookContext(job=job, run=run, phase="on_failure", ctx=ctx, error=error))
            self.hooks.fire(HookContext(job=job, run=run, phase="post_job", ctx=ctx))
            return run

        await self._execute_workflow_job(job, run, ctx, trigger)
        self.hooks.fire(HookContext(job=job, run=run, phase="post_job", ctx=ctx))
        return run

    async def _execute_workflow_job(
        self,
        job: JobDefinition,
        run: RunRecord,
        ctx: StepContext,
        trigger: str,
    ) -> None:
        from trade_compass_agent.runtime.workflows.engine import run_workflow_asset_by_id

        step_rec = self.run_store.create_step_run(run.id, "workflow")
        self.run_store.start_step(step_rec)
        workflow_inputs = _workflow_job_inputs(job, run, ctx)
        try:
            output = await asyncio.wait_for(
                asyncio.to_thread(
                    run_workflow_asset_by_id,
                    job.workflow_id,
                    workflow_inputs,
                    config=self.config,
                    data_dir=self.config.data_dir,
                    trigger=f"{trigger}:{job.id}",
                    run_id=run.id,
                ),
                timeout=job.timeout_seconds,
            )
            analysis = extract_analysis_from_workflow_output(output)
            data = {
                "workflow_id": job.workflow_id,
                "run_id": output.get("run_id"),
                "artifact_id": output.get("artifact_id"),
                "warnings": output.get("warnings", []),
                "trace_path": str(self.config.data_dir / "workflow_runs" / run.id / "trace.jsonl"),
                "schedule_binding": {
                    "job_id": job.id,
                    "workflow_id": job.workflow_id,
                },
            }
            if analysis:
                data["analysis"] = analysis
            self.run_store.complete_step(
                step_rec,
                output=f"workflow {job.workflow_id} completed",
                data_json=json.dumps(data, ensure_ascii=False, default=str),
            )
            message = workflow_run_message(job.workflow_id, output, analysis)
            ctx.upstream["workflow"] = StepOutput(message=message, data=data)
            artifact = _first_artifact_path(self.config.data_dir, run.id)
            if output.get("degraded"):
                degradation_error = str(output.get("error") or message or "workflow degraded")
                self.run_store.degrade_run(
                    run,
                    error=degradation_error,
                    message=message,
                    artifact=artifact,
                )
            else:
                self.run_store.complete_run(run, message=message, artifact=artifact)
            if job.agent_session and not output.get("degraded"):
                self.reflection.store_pending(job.id, run.id, predictions={"workflow": output}, summary=run.message)
        except asyncio.TimeoutError:
            self.run_store.fail_step(step_rec, error=f"Workflow {job.workflow_id} timed out")
            self.run_store.timeout_run(run)
            self.hooks.fire(HookContext(job=job, run=run, phase="on_failure", ctx=ctx, error="timeout"))
        except Exception as exc:
            self.run_store.fail_step(step_rec, error=str(exc))
            self.run_store.fail_run(run, error=str(exc), message=f"Workflow 执行失败: {exc}")
            self.hooks.fire(HookContext(job=job, run=run, phase="on_failure", ctx=ctx, error=str(exc)))

def _workflow_job_inputs(job: JobDefinition, run: RunRecord, ctx: StepContext) -> dict[str, Any]:
    inputs = {
        "as_of": ctx.date.isoformat(),
        "run_id": run.id,
        "source_refs": [f"scheduler:{job.id}:{job.workflow_id}"],
    }
    for key, value in job.workflow_inputs.items():
        if value == "{date}":
            inputs[key] = ctx.date.isoformat()
        elif value == "{run_id}":
            inputs[key] = run.id
        else:
            inputs[key] = value
    return inputs


def _first_artifact_path(data_dir, run_id: str) -> str | None:
    path = data_dir / "workflow_runs" / run_id / "run.json"
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    artifact_paths = payload.get("artifact_paths") or []
    if artifact_paths:
        return str(artifact_paths[0])
    return None


def _is_trading_day() -> bool:
    from trade_compass_agent.ops.trading_calendar import is_trading_day
    return is_trading_day()
