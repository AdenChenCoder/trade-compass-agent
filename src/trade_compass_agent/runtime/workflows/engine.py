from __future__ import annotations

import json
import hashlib
import uuid
import time
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from trade_compass_agent.config import (
    PACKAGE_ROOT,
    PROJECT_ROOT,
    AppConfig,
    load_app_config,
    resolve_schema_path,
)
from trade_compass_agent.runtime.market_stack import MarketStack
from trade_compass_agent.runtime.run_trace import TurnTraceWriter
from trade_compass_agent.runtime.schema_validator import validate_schema
from trade_compass_agent.runtime.specialists.asset_runner import structure_specialist_output
from trade_compass_agent.runtime.specialists.assets import load_specialist_profiles
from trade_compass_agent.runtime.specialists.run import run_specialist
from trade_compass_agent.runtime.tools.policy import default_tool_policy
from trade_compass_agent.runtime.tools.readers import run_reader_tool
from trade_compass_agent.runtime.tools.registry import ToolRegistry
from trade_compass_agent.runtime.types import TurnEvent

BUILTIN_WORKFLOW_DIR = PACKAGE_ROOT / "workflows"


class WorkflowError(ValueError):
    pass


@dataclass(frozen=True)
class WorkflowStep:
    id: str
    type: str
    uses: str
    depends_on: tuple[str, ...] = ()
    with_inputs: dict[str, Any] = field(default_factory=dict)
    timeout_seconds: int | None = None
    persist_artifact: bool = False
    primary_output: bool = False
    when: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class WorkflowManifest:
    id: str
    version: int
    name: str
    description: str
    owner: str
    inputs: dict[str, Any]
    steps: tuple[WorkflowStep, ...]
    output_schema: str
    risk_policy: dict[str, Any]
    persistence: dict[str, Any] = field(default_factory=dict)
    timeout_seconds: int = 300
    retry_policy: dict[str, Any] = field(default_factory=dict)
    degradation_policy: dict[str, Any] = field(default_factory=dict)
    evaluation_hooks: tuple[str, ...] = ()
    path: Path | None = None


@dataclass(frozen=True)
class WorkflowStepResult:
    step_id: str
    type: str
    uses: str
    output: Any
    warnings: tuple[str, ...] = ()

    def model_dump(self) -> dict[str, Any]:
        return json.loads(json.dumps(asdict(self), ensure_ascii=False, default=str))


@dataclass(frozen=True)
class WorkflowRunContext:
    run_id: str
    trigger: str
    data_dir: Path
    trace_writer: TurnTraceWriter
    started_at: str
    inputs_hash: str
    cancelled: threading.Event = field(default_factory=threading.Event, compare=False, repr=False)

    @property
    def trace_path(self) -> Path:
        return self.trace_writer.trace_path

    def record(self, event: str, data: dict[str, Any] | None = None) -> None:
        self.trace_writer.record(TurnEvent(event=event, data=data or {}))


REQUIRED_V2_WORKFLOW_FIELDS = (
    "id",
    "version",
    "name",
    "description",
    "owner",
    "inputs",
    "steps",
    "output_schema",
    "persistence",
    "risk_policy",
    "timeout_seconds",
    "retry_policy",
    "degradation_policy",
    "evaluation_hooks",
)

SUPPORTED_STEP_TYPES = {"tool", "specialist", "workflow", "compose", "evaluate"}
MAX_SUBWORKFLOW_DEPTH = 3


def load_workflow_assets(directory: Path = BUILTIN_WORKFLOW_DIR) -> dict[str, WorkflowManifest]:
    workflows: dict[str, WorkflowManifest] = {}
    if not directory.is_dir():
        return workflows
    for path in sorted(directory.glob("*/workflow.yaml")):
        manifest = load_workflow_asset(path)
        if manifest.id in workflows:
            raise WorkflowError(f"duplicate workflow id: {manifest.id}")
        workflows[manifest.id] = manifest
    return workflows


def load_workflow_asset(path: Path) -> WorkflowManifest:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise WorkflowError(f"{path}: workflow manifest must be an object")
    missing = [field for field in REQUIRED_V2_WORKFLOW_FIELDS if field not in raw]
    if missing:
        raise WorkflowError(f"{path}: missing required fields: {', '.join(missing)}")
    workflow_id = str(raw["id"])
    if path.parent.name != workflow_id:
        raise WorkflowError(f"{path}: workflow id must match folder name")
    steps = tuple(_load_step(item, path) for item in raw.get("steps") or [])
    _validate_dag(workflow_id, steps)
    return WorkflowManifest(
        id=workflow_id,
        version=int(raw["version"]),
        name=str(raw["name"]),
        description=str(raw["description"]),
        owner=str(raw["owner"]),
        inputs=dict(raw.get("inputs") or {}),
        steps=steps,
        output_schema=str(raw["output_schema"]),
        risk_policy=dict(raw.get("risk_policy") or {}),
        persistence=dict(raw.get("persistence") or {}),
        timeout_seconds=int(raw.get("timeout_seconds") or 300),
        retry_policy=dict(raw.get("retry_policy") or {}),
        degradation_policy=dict(raw.get("degradation_policy") or {}),
        evaluation_hooks=tuple(str(item) for item in raw.get("evaluation_hooks") or ()),
        path=path,
    )


def run_workflow_asset(
    manifest: WorkflowManifest,
    inputs: dict[str, Any],
    *,
    config: AppConfig | None = None,
    stack: MarketStack | None = None,
    persist: bool = True,
    data_dir: Path | None = None,
    trigger: str = "runtime",
    run_id: str | None = None,
    _depth: int = 0,
    _visited: tuple[str, ...] = (),
) -> dict[str, Any]:
    _validate_inputs(manifest, inputs)
    if _depth > MAX_SUBWORKFLOW_DEPTH:
        raise WorkflowError(f"workflow nesting exceeds max depth: {MAX_SUBWORKFLOW_DEPTH}")
    if manifest.id in _visited:
        chain = " -> ".join((*_visited, manifest.id))
        raise WorkflowError(f"workflow cycle detected: {chain}")
    app_config = config or load_app_config()
    market_stack = stack or MarketStack.from_config(app_config)
    root = data_dir or app_config.data_dir
    workflow_run_id = run_id or str(inputs.get("run_id") or uuid.uuid4().hex)
    context = WorkflowRunContext(
        run_id=workflow_run_id,
        trigger=trigger,
        data_dir=root,
        trace_writer=TurnTraceWriter(root / "workflow_runs" / workflow_run_id),
        started_at=_utc_now(),
        inputs_hash=_hash(inputs),
    )
    context.record("builtin.started", {"workflow_id": manifest.id, "trigger": trigger})
    outputs: dict[str, WorkflowStepResult] = {}
    warnings: list[str] = []
    artifact_paths: list[str] = []
    status = "completed"
    error = ""
    try:
        _execute_steps_with_retry(
            manifest,
            inputs,
            outputs,
            warnings,
            artifact_paths,
            market_stack,
            app_config,
            context,
            data_dir=root,
            persist=persist,
            trigger=trigger,
            depth=_depth,
            visited=(*_visited, manifest.id),
        )
        output = _with_metadata(manifest, inputs, _compose_workflow_output(manifest, inputs, outputs, warnings), context)
        validate_workflow_asset_output(manifest, output)
        context.record("builtin.schema_validated", {"workflow_id": manifest.id})
        if persist:
            artifact = persist_workflow_asset_output(manifest, output, data_dir=root)
            if artifact is not None:
                artifact_paths.append(str(artifact))
                context.record("builtin.artifact_written", {"path": str(artifact)})
        return output
    except Exception as exc:
        status = "failed"
        error = f"{type(exc).__name__}: {exc}"
        context.record("builtin.failed", {"workflow_id": manifest.id, "error": error})
        if not _should_degrade(manifest):
            raise
        output = _degraded_output(manifest, inputs, context, error, warnings)
        validate_workflow_asset_output(manifest, output)
        if persist:
            artifact = persist_workflow_asset_output(manifest, output, data_dir=root)
            if artifact is not None:
                artifact_paths.append(str(artifact))
                context.record("builtin.degraded_artifact_written", {"path": str(artifact)})
        return output
    finally:
        _persist_run_record(
            context,
            {
                "run_id": context.run_id,
                "workflow_id": manifest.id,
                "workflow_version": manifest.version,
                "trigger": trigger,
                "status": status,
                "started_at": context.started_at,
                "finished_at": _utc_now(),
                "inputs_hash": context.inputs_hash,
                "artifact_paths": artifact_paths,
                "trace_path": str(context.trace_path),
                "warnings": warnings,
                "error": error,
            },
        )


def _execute_steps_with_retry(
    manifest: WorkflowManifest,
    inputs: dict[str, Any],
    outputs: dict[str, WorkflowStepResult],
    warnings: list[str],
    artifact_paths: list[str],
    stack: MarketStack,
    config: AppConfig,
    context: WorkflowRunContext,
    *,
    data_dir: Path,
    persist: bool,
    trigger: str,
    depth: int,
    visited: tuple[str, ...],
) -> None:
    attempts = 1 + max(0, int(manifest.retry_policy.get("max_retries") or 0))
    backoff = max(0, int(manifest.retry_policy.get("backoff_seconds") or 0))
    last_error: Exception | None = None
    for attempt in range(attempts):
        if attempt:
            context.record("builtin.retry", {"attempt": attempt + 1})
            time.sleep(backoff)
        outputs.clear()
        warnings.clear()
        try:
            _execute_steps_with_timeout(
                manifest,
                inputs,
                outputs,
                warnings,
                artifact_paths,
                stack,
                config,
                context,
                data_dir=data_dir,
                persist=persist,
                trigger=trigger,
                depth=depth,
                visited=visited,
            )
        except Exception as exc:
            last_error = exc
            context.record("builtin.attempt_failed", {"attempt": attempt + 1, "error": str(exc)})
            continue
        return
    assert last_error is not None
    raise last_error


def _execute_steps_with_timeout(
    manifest: WorkflowManifest,
    inputs: dict[str, Any],
    outputs: dict[str, WorkflowStepResult],
    warnings: list[str],
    artifact_paths: list[str],
    stack: MarketStack,
    config: AppConfig,
    context: WorkflowRunContext,
    *,
    data_dir: Path,
    persist: bool,
    trigger: str,
    depth: int,
    visited: tuple[str, ...],
) -> None:
    pool = ThreadPoolExecutor(max_workers=1)
    future = pool.submit(
        _execute_steps_once,
        manifest,
        inputs,
        outputs,
        warnings,
        artifact_paths,
        stack,
        config,
        context,
        data_dir=data_dir,
        persist=persist,
        trigger=trigger,
        depth=depth,
        visited=visited,
    )
    try:
        future.result(timeout=max(1, manifest.timeout_seconds))
    except FutureTimeoutError as exc:
        future.cancel()
        context.cancelled.set()
        context.record("builtin.timeout", {"timeout_seconds": manifest.timeout_seconds})
        raise TimeoutError(f"{manifest.id}: workflow timed out after {manifest.timeout_seconds}s") from exc
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


def _execute_steps_once(
    manifest: WorkflowManifest,
    inputs: dict[str, Any],
    outputs: dict[str, WorkflowStepResult],
    warnings: list[str],
    artifact_paths: list[str],
    stack: MarketStack,
    config: AppConfig,
    context: WorkflowRunContext,
    *,
    data_dir: Path,
    persist: bool,
    trigger: str,
    depth: int,
    visited: tuple[str, ...],
) -> None:
    for step in _topological_steps(manifest.steps):
        if context.cancelled.is_set():
            context.record("builtin.cancelled", {"before_step": step.id})
            return
        context.record("builtin.step_started", {"step_id": step.id, "type": step.type, "uses": step.uses})
        if not _step_condition_met(step, inputs):
            result = WorkflowStepResult(
                step_id=step.id,
                type=step.type,
                uses=step.uses,
                output={"skipped": True, "reason": "input condition not met"},
            )
        else:
            result = _run_step(
                step,
                inputs,
                outputs,
                stack,
                config,
                data_dir=data_dir,
                trigger=trigger,
                parent_run_id=context.run_id,
                depth=depth,
                visited=visited,
                workflow_directory=_workflow_asset_directory(manifest),
                manifest=manifest,
            )
        outputs[step.id] = result
        warnings.extend(result.warnings)
        if step.persist_artifact and persist:
            artifact = persist_workflow_step_artifact(manifest, step, result, inputs, context, data_dir=data_dir)
            artifact_paths.append(str(artifact))
            context.record("builtin.step_artifact_written", {"step_id": step.id, "path": str(artifact)})
        result_output = _output_as_dict(result.output)
        result_error = str(result_output.get("error") or "") if result_output else ""
        context.record(
            "builtin.step_finished",
            {
                "step_id": step.id,
                "warning_count": len(result.warnings),
                "status": "failed" if result_error else "completed",
                **({"error": result_error} if result_error else {}),
            },
        )


def run_workflow_asset_by_id(
    workflow_id: str,
    inputs: dict[str, Any],
    *,
    directory: Path = BUILTIN_WORKFLOW_DIR,
    config: AppConfig | None = None,
    stack: MarketStack | None = None,
    persist: bool = True,
    data_dir: Path | None = None,
    trigger: str = "runtime",
    run_id: str | None = None,
    _depth: int = 0,
    _visited: tuple[str, ...] = (),
) -> dict[str, Any]:
    workflows = load_workflow_assets(directory)
    manifest = workflows.get(workflow_id)
    if manifest is None:
        raise WorkflowError(f"unknown workflow asset: {workflow_id}")
    return run_workflow_asset(
        manifest,
        inputs,
        config=config,
        stack=stack,
        persist=persist,
        data_dir=data_dir,
        trigger=trigger,
        run_id=run_id,
        _depth=_depth,
        _visited=_visited,
    )


def validate_workflow_asset_output(manifest: WorkflowManifest, output: dict[str, Any]) -> None:
    schema_path = _workflow_relative_path(manifest, manifest.output_schema)
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    validate_schema(output, schema)


def persist_workflow_asset_output(
    manifest: WorkflowManifest,
    output: dict[str, Any],
    *,
    data_dir: Path,
) -> Path | None:
    persistence = manifest.persistence or {}
    if persistence.get("kind") != "jsonl":
        return None
    template = str(persistence.get("path_template") or "")
    if not template:
        return None
    rendered = template.format(
        date=str(output.get("as_of", "unknown")),
        mode=str(output.get("mode") or inputs_mode_from_output(output)),
        week=_week_key(str(output.get("as_of", "unknown"))),
        workflow_id=manifest.id,
        run_id=str(output.get("run_id", "")),
    )
    path = data_dir / rendered.removeprefix("data/")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(output, ensure_ascii=False, sort_keys=True) + "\n")
    return path


def _compose_workflow_output(
    manifest: WorkflowManifest,
    inputs: dict[str, Any],
    outputs: dict[str, WorkflowStepResult],
    warnings: list[str],
) -> dict[str, Any]:
    primary_step_id, primary = _primary_step_payload(manifest, outputs)
    primary = {key: value for key, value in primary.items() if key != "steps"}
    merged_warnings = [*warnings]
    if isinstance(primary.get("warnings"), list):
        merged_warnings.extend(str(item) for item in primary["warnings"])
    if primary.get("error"):
        merged_warnings.append(f"primary output step failed: {primary_step_id}: {primary['error']}")
    return {
        **primary,
        "workflow_id": manifest.id,
        "workflow_version": manifest.version,
        "as_of": str(primary.get("as_of") or inputs.get("as_of") or ""),
        "primary_step_id": primary_step_id,
        "warnings": list(dict.fromkeys(merged_warnings)),
        "no_trade_disclaimer": True,
        **({"degraded": True} if primary.get("error") else {}),
    }


def _primary_step_payload(
    manifest: WorkflowManifest,
    outputs: dict[str, WorkflowStepResult],
) -> tuple[str, dict[str, Any]]:
    primary_step_ids = {step.id for step in manifest.steps if step.primary_output}
    for result in reversed(list(outputs.values())):
        if result.step_id not in primary_step_ids:
            continue
        output = _output_as_dict(result.output)
        if output and not output.get("skipped"):
            return result.step_id, output
    for result in reversed(list(outputs.values())):
        output = _output_as_dict(result.output)
        if output and not output.get("skipped"):
            return result.step_id, output
    return "", {}

def _output_as_dict(output: Any) -> dict[str, Any]:
    if isinstance(output, dict):
        return dict(output)
    if isinstance(output, str):
        try:
            parsed = json.loads(output)
        except json.JSONDecodeError:
            return {}
        if isinstance(parsed, dict):
            return parsed
    return {}


def _step_condition_met(step: WorkflowStep, inputs: dict[str, Any]) -> bool:
    condition = step.when or {}
    input_name = str(condition.get("input_present") or "")
    if input_name:
        value = inputs.get(input_name)
        if value is None:
            return False
        if isinstance(value, str) and not value.strip():
            return False
        if isinstance(value, list | tuple | dict) and not value:
            return False
    return True


def inputs_mode_from_output(output: dict[str, Any]) -> str:
    return str(output.get("mode") or "manual")


def _week_key(value: str) -> str:
    try:
        from datetime import date

        day = date.fromisoformat(value)
    except ValueError:
        return value
    iso = day.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def persist_workflow_step_artifact(
    manifest: WorkflowManifest,
    step: WorkflowStep,
    result: WorkflowStepResult,
    inputs: dict[str, Any],
    context: WorkflowRunContext,
    *,
    data_dir: Path,
) -> Path:
    as_of = str(inputs.get("as_of") or "unknown")
    record = {
        "artifact_id": _hash(
            {
                "workflow_id": manifest.id,
                "run_id": context.run_id,
                "step_id": step.id,
                "output": result.output,
            }
        ),
        "workflow_id": manifest.id,
        "workflow_version": manifest.version,
        "run_id": context.run_id,
        "step_id": step.id,
        "step_type": step.type,
        "uses": step.uses,
        "created_at": _utc_now(),
        "as_of": as_of,
        "schema_version": 2,
        "inputs_hash": context.inputs_hash,
        "source_refs": _source_refs(inputs, result.model_dump()),
        "warnings": list(result.warnings),
        "output": result.output,
    }
    path = data_dir / "workflows" / manifest.id / "steps" / step.id / f"{as_of}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    return path


def _load_step(raw: Any, path: Path) -> WorkflowStep:
    if not isinstance(raw, dict):
        raise WorkflowError(f"{path}: workflow step must be object")
    step_id = str(raw.get("id") or "")
    step_type = str(raw.get("type") or "")
    uses = str(raw.get("uses") or "")
    if not step_id:
        raise WorkflowError(f"{path}: workflow step missing id")
    if step_type not in SUPPORTED_STEP_TYPES:
        raise WorkflowError(f"{path}: step {step_id} has unsupported type: {step_type}")
    if not uses:
        raise WorkflowError(f"{path}: step {step_id} missing uses")
    depends_on = raw.get("depends_on") or ()
    if not isinstance(depends_on, list | tuple):
        raise WorkflowError(f"{path}: step {step_id} depends_on must be list")
    with_inputs = raw.get("with") or {}
    if not isinstance(with_inputs, dict):
        raise WorkflowError(f"{path}: step {step_id} with must be object")
    return WorkflowStep(
        id=step_id,
        type=step_type,
        uses=uses,
        depends_on=tuple(str(item) for item in depends_on),
        with_inputs=dict(with_inputs),
        timeout_seconds=_optional_positive_int(raw.get("timeout_seconds"), path, f"step {step_id} timeout_seconds"),
        persist_artifact=bool(raw.get("persist_artifact", False)),
        primary_output=bool(raw.get("primary_output", False)),
        when=dict(raw.get("when") or {}),
    )


def _validate_inputs(manifest: WorkflowManifest, inputs: dict[str, Any]) -> None:
    required = [str(item) for item in manifest.inputs.get("required") or []]
    missing = [item for item in required if item not in inputs]
    if missing:
        raise WorkflowError(f"{manifest.id}: missing workflow inputs: {', '.join(missing)}")


def _optional_positive_int(value: Any, path: Path, field_name: str) -> int | None:
    if value is None:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise WorkflowError(f"{path}: {field_name} must be a positive integer") from exc
    if parsed <= 0:
        raise WorkflowError(f"{path}: {field_name} must be a positive integer")
    return parsed


def _validate_dag(workflow_id: str, steps: tuple[WorkflowStep, ...]) -> None:
    ids: set[str] = set()
    for step in steps:
        if step.id in ids:
            raise WorkflowError(f"{workflow_id}: duplicate step id: {step.id}")
        ids.add(step.id)
    for step in steps:
        for dependency in step.depends_on:
            if dependency not in ids:
                raise WorkflowError(f"{workflow_id}: step {step.id} depends on unknown step: {dependency}")
    _topological_steps(steps)


def _topological_steps(steps: tuple[WorkflowStep, ...]) -> list[WorkflowStep]:
    by_id = {step.id: step for step in steps}
    pending = set(by_id)
    resolved: set[str] = set()
    ordered: list[WorkflowStep] = []
    while pending:
        ready = sorted(
            step_id for step_id in pending if set(by_id[step_id].depends_on).issubset(resolved)
        )
        if not ready:
            cycle = ", ".join(sorted(pending))
            raise WorkflowError(f"workflow step cycle detected: {cycle}")
        for step_id in ready:
            pending.remove(step_id)
            resolved.add(step_id)
            ordered.append(by_id[step_id])
    return ordered


def _run_step(
    step: WorkflowStep,
    inputs: dict[str, Any],
    outputs: dict[str, WorkflowStepResult],
    stack: MarketStack,
    config: AppConfig,
    *,
    data_dir: Path,
    trigger: str,
    parent_run_id: str,
    depth: int,
    visited: tuple[str, ...],
    workflow_directory: Path,
    manifest: WorkflowManifest,
) -> WorkflowStepResult:
    if step.type == "specialist":
        specialist_id = _prefixed_id(step.uses, "specialist")
        task = _render_value(step.with_inputs.get("task") or "{inputs}", inputs, outputs)
        raw_output = run_specialist(stack, specialist_id, task, config=config)
        profile = load_specialist_profiles().get(specialist_id)
        output = structure_specialist_output(profile, raw_output) if profile is not None else raw_output
        return WorkflowStepResult(step_id=step.id, type=step.type, uses=step.uses, output=output)
    if step.type == "workflow":
        workflow_id = _prefixed_id(step.uses, "workflow")
        child_inputs = {
            key: _render_typed_value(value, inputs, outputs)
            for key, value in step.with_inputs.items()
        }
        if "as_of" not in child_inputs and "as_of" in inputs:
            child_inputs["as_of"] = inputs["as_of"]
        child_output = run_workflow_asset_by_id(
            workflow_id,
            child_inputs,
            directory=workflow_directory,
            config=config,
            stack=stack,
            data_dir=data_dir,
            trigger=f"{trigger}:subworkflow:{step.id}",
            run_id=f"{parent_run_id}-{step.id}",
            _depth=depth + 1,
            _visited=visited,
        )
        return WorkflowStepResult(
            step_id=step.id,
            type=step.type,
            uses=step.uses,
            output=child_output,
        )
    if step.type == "tool":
        tool_id = _prefixed_id(step.uses, "tool")
        descriptor = default_tool_policy().resolve(tool_id)
        args = {
            key: _render_typed_value(value, inputs, outputs)
            for key, value in step.with_inputs.items()
        }
        if step.timeout_seconds is not None:
            args.setdefault("step_timeout_seconds", step.timeout_seconds)
        if descriptor.category == "reader":
            output = run_reader_tool(tool_id, **args)
            return WorkflowStepResult(step_id=step.id, type=step.type, uses=step.uses, output=output)
        output = ToolRegistry(stack).execute(tool_id, args)
        return WorkflowStepResult(step_id=step.id, type=step.type, uses=step.uses, output=output)
    if step.type == "compose":
        return WorkflowStepResult(
            step_id=step.id,
            type=step.type,
            uses=step.uses,
            output={
                "inputs": dict(inputs),
                "steps": {step_id: result.output for step_id, result in outputs.items()},
            },
        )
    if step.type == "evaluate":
        return WorkflowStepResult(
            step_id=step.id,
            type=step.type,
            uses=step.uses,
            output={
                "workflow_id": manifest.id,
                "hooks": list(manifest.evaluation_hooks),
                "status": "pending",
                "evaluated_steps": list(outputs),
            },
            warnings=("evaluation step recorded as pending until post-run evaluator executes",),
        )
    raise WorkflowError(f"unsupported step type: {step.type}")


def _with_metadata(
    manifest: WorkflowManifest,
    inputs: dict[str, Any],
    output: dict[str, Any],
    context: WorkflowRunContext,
) -> dict[str, Any]:
    result = dict(output)
    result["run_id"] = str(result.get("run_id") or context.run_id)
    result.setdefault("artifact_id", _hash({"workflow_id": manifest.id, "run_id": context.run_id, "output": output}))
    result.setdefault("created_at", _utc_now())
    result["schema_version"] = 2
    result.setdefault("inputs_hash", context.inputs_hash)
    result["source_refs"] = list(
        dict.fromkeys(
            [
                *[str(item) for item in result.get("source_refs") or [] if str(item).strip()],
                *_source_refs(inputs, result),
            ]
        )
    )
    result.setdefault("evaluation_status", "pending")
    return result


def _degraded_output(
    manifest: WorkflowManifest,
    inputs: dict[str, Any],
    context: WorkflowRunContext,
    error: str,
    warnings: list[str],
) -> dict[str, Any]:
    degraded_warning = f"workflow degraded: {error}"
    merged_warnings = [*warnings, degraded_warning]
    return _with_metadata(
        manifest,
        inputs,
        {
            "workflow_id": manifest.id,
            "workflow_version": manifest.version,
            "as_of": str(inputs.get("as_of") or "unknown"),
            "warnings": merged_warnings,
            "no_trade_disclaimer": True,
            "degraded": True,
        },
        context,
    )


def _should_degrade(manifest: WorkflowManifest) -> bool:
    policy = manifest.degradation_policy or {}
    return str(policy.get("on_failure") or policy.get("on_partial_data") or "") in {
        "emit_degraded_artifact",
        "emit_with_warnings",
    }


def _source_refs(inputs: dict[str, Any], output: dict[str, Any]) -> list[str]:
    refs: list[str] = []
    refs.extend(_collect_source_refs(inputs))
    refs.extend(_collect_source_refs(output))
    return list(dict.fromkeys(refs))


def _collect_source_refs(value: Any, *, limit: int = 200) -> list[str]:
    refs: list[str] = []

    def visit(item: Any) -> None:
        if len(refs) >= limit:
            return
        if isinstance(item, dict):
            raw = item.get("source_refs")
            if isinstance(raw, str) and raw.strip():
                refs.append(raw)
            elif isinstance(raw, list | tuple):
                refs.extend(str(x) for x in raw if str(x).strip())
            source = item.get("source")
            if isinstance(source, str) and source.strip():
                refs.append(source)
            for nested in item.values():
                visit(nested)
        elif isinstance(item, str):
            try:
                parsed = json.loads(item)
            except json.JSONDecodeError:
                return
            visit(parsed)
        elif isinstance(item, list | tuple):
            for nested in item:
                visit(nested)

    visit(value)
    return refs[:limit]


def _workflow_relative_path(manifest: WorkflowManifest, raw_path: str) -> Path:
    if manifest.path is None:
        return PROJECT_ROOT / raw_path
    candidate = manifest.path.parent / raw_path
    if candidate.is_file():
        return candidate
    package_prefix = "src/trade_compass_agent/"
    if raw_path.startswith(package_prefix):
        packaged = PACKAGE_ROOT / raw_path.removeprefix(package_prefix)
        if packaged.is_file():
            return packaged
    if raw_path.startswith("schemas/"):
        return resolve_schema_path(raw_path)
    return PROJECT_ROOT / raw_path


def _workflow_asset_directory(manifest: WorkflowManifest) -> Path:
    if manifest.path is None:
        return BUILTIN_WORKFLOW_DIR
    return manifest.path.parent.parent


def _persist_run_record(context: WorkflowRunContext, record: dict[str, Any]) -> Path:
    run_dir = context.data_dir / "workflow_runs" / context.run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / "run.json"
    path.write_text(
        json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:24]


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _prefixed_id(value: str, prefix: str) -> str:
    expected = f"{prefix}:"
    if not value.startswith(expected):
        raise WorkflowError(f"expected {expected} reference, got {value!r}")
    return value.removeprefix(expected)


def _render_value(template: Any, inputs: dict[str, Any], outputs: dict[str, WorkflowStepResult]) -> str:
    if not isinstance(template, str):
        return json.dumps(template, ensure_ascii=False, default=str)
    if template == "{inputs}":
        return json.dumps(inputs, ensure_ascii=False, default=str)
    rendered = template
    for key, value in inputs.items():
        rendered = rendered.replace("{" + f"inputs.{key}" + "}", str(value))
    for key, value in outputs.items():
        rendered = rendered.replace("{" + f"steps.{key}.output" + "}", str(value.output))
    return rendered


def _render_typed_value(template: Any, inputs: dict[str, Any], outputs: dict[str, WorkflowStepResult]) -> Any:
    if isinstance(template, dict):
        return {key: _render_typed_value(value, inputs, outputs) for key, value in template.items()}
    if isinstance(template, list):
        return [_render_typed_value(value, inputs, outputs) for value in template]
    if template == "{inputs}":
        return inputs
    if isinstance(template, str) and template.startswith("{inputs.") and template.endswith("}"):
        path = template.removeprefix("{inputs.").removesuffix("}")
        return _resolve_path(inputs, path)
    if isinstance(template, str) and template.startswith("{steps.") and template.endswith(".output}"):
        key = template.removeprefix("{steps.").removesuffix(".output}")
        if key in outputs:
            return outputs[key].output
        return ""
    if isinstance(template, str) and template.startswith("{steps.") and template.endswith("}"):
        path = template.removeprefix("{steps.").removesuffix("}")
        step_id, _, nested = path.partition(".output.")
        if step_id in outputs and nested:
            return _resolve_path(outputs[step_id].output, nested)
        return ""
    rendered = _render_value(template, inputs, outputs)
    if isinstance(template, str) and template.startswith("{") and template.endswith("}"):
        try:
            return json.loads(rendered)
        except json.JSONDecodeError:
            return rendered
    return rendered


def _resolve_path(value: Any, path: str) -> Any:
    current = value
    for part in path.split("."):
        if isinstance(current, dict):
            current = current.get(part, "")
        elif isinstance(current, list):
            try:
                current = current[int(part)]
            except (ValueError, IndexError):
                return ""
        else:
            return ""
    return current
