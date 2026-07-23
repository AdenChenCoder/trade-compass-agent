from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from trade_compass_agent.runtime.workflows.engine import (
    WorkflowStep,
    WorkflowStepResult,
    WorkflowError,
    WorkflowManifest,
    WorkflowRunContext,
    load_workflow_asset,
    load_workflow_assets,
    run_workflow_asset,
    run_workflow_asset_by_id,
    validate_workflow_asset_output,
)


def list_workflow_runs(
    data_dir: Path,
    *,
    workflow_id: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    root = data_dir / "workflow_runs"
    if not root.is_dir():
        return []
    records: list[dict[str, Any]] = []
    for path in sorted(root.glob("*/run.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if workflow_id and record.get("workflow_id") != workflow_id:
            continue
        records.append(record)
    records.sort(key=lambda item: str(item.get("started_at") or item.get("finished_at") or ""))
    return records[-limit:]


__all__ = [
    "WorkflowStep",
    "WorkflowStepResult",
    "WorkflowError",
    "WorkflowManifest",
    "WorkflowRunContext",
    "list_workflow_runs",
    "load_workflow_asset",
    "load_workflow_assets",
    "run_workflow_asset",
    "run_workflow_asset_by_id",
    "validate_workflow_asset_output",
]
