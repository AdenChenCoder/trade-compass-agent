from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from trade_compass_agent.ops.job_definition import StepContext, StepOutput
from trade_compass_agent.evaluation.workflow_artifacts import WorkflowArtifactEvaluator
from trade_compass_agent.runtime.market_stack import MarketStack


async def update_artifact_tracking(ctx: StepContext) -> StepOutput:
    """Summarize durable catalyst and idea artifacts for EOD/weekly review."""
    days = _tracking_days(ctx)
    catalyst_rows = [
        row
        for day in days
        for row in _read_jsonl(_artifact_path(ctx.config.data_dir, "catalysts", day))
    ]
    idea_rows = [
        row
        for day in days
        for path in _idea_artifact_paths(ctx.config.data_dir, day)
        for row in _read_jsonl(path)
    ]
    catalyst_count = sum(len(row.get("events", [])) for row in catalyst_rows)
    idea_count = sum(len(row.get("ideas", [])) for row in idea_rows)
    evaluation = _evaluate_workflow_artifacts(ctx)
    return StepOutput(
        message=f"研究资产追踪: {catalyst_count} 个催化剂, {idea_count} 个候选",
        data={
            "catalyst_artifacts": len(catalyst_rows),
            "idea_artifacts": len(idea_rows),
            "catalyst_events": catalyst_count,
            "ideas": idea_count,
            "days": [day.isoformat() for day in days],
            "evaluation": evaluation,
        },
    )


def _artifact_path(data_dir: Path, kind: str, day: date) -> Path:
    return data_dir / kind / f"{day.isoformat()}.jsonl"


def _idea_artifact_paths(data_dir: Path, day: date) -> list[Path]:
    root = data_dir / "ideas"
    paths = [
        root / f"{day.isoformat()}-morning.jsonl",
        root / f"{_week_key(day)}-weekend.jsonl",
        root / f"{day.isoformat()}-weekly.jsonl",
        root / f"{day.isoformat()}-manual.jsonl",
        root / f"{day.isoformat()}.jsonl",
    ]
    return paths


def _week_key(day: date) -> str:
    iso = day.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def _tracking_days(ctx: StepContext) -> list[date]:
    if ctx.job_id == "weekly":
        return [ctx.date - timedelta(days=offset) for offset in range(6, -1, -1)]
    return [ctx.date]


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _evaluate_workflow_artifacts(ctx: StepContext) -> dict[str, Any]:
    try:
        provider = MarketStack.from_config(ctx.config).provider
    except Exception:
        provider = None
    evaluator = WorkflowArtifactEvaluator(ctx.config.data_dir, provider=provider)
    lookback = 7 if ctx.job_id == "weekly" else 1
    report = evaluator.evaluate(as_of=ctx.date, lookback_days=lookback)
    path = evaluator.persist(report)
    data = report.model_dump()
    data["artifact"] = str(path)
    return data
