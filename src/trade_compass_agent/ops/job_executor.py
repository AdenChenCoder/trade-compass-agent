"""JobExecutor — scheduler binding executor.

Built-in scheduler jobs bind to workflow assets. The workflow runtime owns the
business step DAG; the scheduler only starts the requested workflow and records
the run.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
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
        as_of: date | None = None,
    ) -> RunRecord:
        run = self.run_store.create_run(job.id, trigger=trigger)

        from trade_compass_agent.ops.autonomous_trading import JOB_ID, skip_reason
        if job.id == JOB_ID:
            reason = skip_reason(self.config.data_dir)
            if reason:
                self.run_store.skip_run(run, reason=reason)
                return run

        if trigger not in {"api", "cli"} and job.trading_day_only and not _is_trading_day():
            self.run_store.skip_run(run, reason="非交易日")
            return run

        if not self.run_store.start_run_if_idle(run):
            self.run_store.skip_run(run, reason="同一 Job 正在运行（overlap guard）")
            return run

        deadline = time.monotonic() + job.timeout_seconds

        # Resolve stale pending reflections, then inject past lessons
        if job.agent_session:
            from trade_compass_agent.memory.memory_store import MemoryStore
            from trade_compass_agent.ops.reflection_resolver import make_market_resolve_fn

            mem_store = MemoryStore(
                self.config.memory_dir,
                min_inject_confidence=self.config.memory.governance.min_inject_confidence,
            )
            from trade_compass_agent.llm.providers import LLMExecutionCancelled, llm_request_deadline
            from trade_compass_agent.data.network import drain_timeout_workers, run_with_timeout

            stopped = threading.Event()

            def resolve_reflections():
                with llm_request_deadline(deadline, is_cancelled=stopped.is_set):
                    self.reflection.resolve_pending(
                        job.id, resolve_fn=make_market_resolve_fn(self.config),
                        mem_store=mem_store, config=self.config,
                    )

            def bounded_reflections():
                with drain_timeout_workers():
                    return run_with_timeout(
                        resolve_reflections, max(.001, deadline - time.monotonic()),
                        "Resolve pending reflections", is_cancelled=stopped.is_set,
                        on_timeout=stopped.set,
                    )

            try:
                await asyncio.to_thread(bounded_reflections)
            except (LLMExecutionCancelled, TimeoutError):
                stopped.set()
                self.run_store.timeout_run(run)
                return run
            except asyncio.CancelledError:
                stopped.set()
                self.run_store.fail_run(run, error="cancelled", message="任务已取消")
                raise
            if reflection_context is None:
                reflection_context = self.reflection.get_context(
                    job.id,
                    limit=5,
                    sanitize=self.config.memory.recall.sanitize_reflections,
                ) or None

        ctx = StepContext(
            config=self.config,
            date=as_of or date.today(),
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

        await self._execute_workflow_job(job, run, ctx, trigger, deadline)
        self.hooks.fire(HookContext(job=job, run=run, phase="post_job", ctx=ctx))
        return run

    async def _execute_workflow_job(
        self,
        job: JobDefinition,
        run: RunRecord,
        ctx: StepContext,
        trigger: str,
        deadline: float,
    ) -> None:
        from trade_compass_agent.runtime.workflows.engine import run_workflow_asset_by_id
        from trade_compass_agent.llm.providers import llm_request_deadline

        step_rec = self.run_store.create_step_run(run.id, "workflow")
        self.run_store.start_step(step_rec)
        workflow_inputs = _workflow_job_inputs(job, run, ctx)
        cancel_event = threading.Event()

        def execute_workflow():
            with llm_request_deadline(deadline, is_cancelled=cancel_event.is_set):
                return run_workflow_asset_by_id(
                    job.workflow_id, workflow_inputs, config=self.config,
                    data_dir=self.config.data_dir, trigger=f"{trigger}:{job.id}",
                    run_id=run.id, cancel_event=cancel_event,
                )

        def bounded_workflow():
            from trade_compass_agent.data.network import drain_timeout_workers, run_with_timeout
            with drain_timeout_workers():
                return run_with_timeout(
                    execute_workflow, max(.001, deadline - time.monotonic()),
                    f"Workflow {job.workflow_id}", is_cancelled=cancel_event.is_set,
                    on_timeout=cancel_event.set,
                )

        # The asyncio executor only waits for this bounded wrapper. The actual
        # workflow has cancellation-aware commit guards and runs on a daemon.
        workflow_task = asyncio.create_task(asyncio.to_thread(bounded_workflow))
        try:
            done, _pending = await asyncio.wait({workflow_task}, timeout=max(0, deadline - time.monotonic()))
            if not done:
                raise TimeoutError("Workflow scheduler deadline reached")
            output = workflow_task.result()
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
            cancel_event.set()
            try:
                await workflow_task
            except Exception:
                pass
            self.run_store.fail_step(step_rec, error=f"Workflow {job.workflow_id} timed out")
            self.run_store.timeout_run(run)
            self.hooks.fire(HookContext(job=job, run=run, phase="on_failure", ctx=ctx, error="timeout"))
        except asyncio.CancelledError:
            cancel_event.set()
            try:
                await workflow_task
            except Exception:
                pass
            error = f"Workflow {job.workflow_id} cancelled"
            self.run_store.fail_step(step_rec, error=error)
            self.run_store.fail_run(run, error=error, message="任务已取消")
            raise
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
