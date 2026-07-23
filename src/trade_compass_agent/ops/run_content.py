"""Helpers for extracting user-facing content from scheduler/workflow runs."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def extract_analysis_from_workflow_output(output: Any) -> str | None:
    """Return the primary human analysis text from a workflow output payload."""
    return _extract_analysis(output)


def extract_analysis_from_step_data(raw: str | None) -> str | None:
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    return _extract_analysis(data)


def extract_analysis_from_artifact(path: str | None, *, run_id: str | None = None) -> str | None:
    if not path:
        return None
    artifact = Path(path)
    if not artifact.is_file():
        return None
    try:
        lines = artifact.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if run_id and str(payload.get("run_id", "")) != run_id:
            continue
        analysis = _extract_analysis(payload)
        if analysis:
            return analysis
    return None


def workflow_run_message(workflow_id: str, output: dict[str, Any], analysis: str | None) -> str:
    headline = _first_markdown_heading(analysis)
    if headline:
        return f"{workflow_id}: {headline}"
    primary_step_id = output.get("primary_step_id")
    error = output.get("error")
    if isinstance(error, str) and error.strip():
        step = f"{primary_step_id} " if isinstance(primary_step_id, str) and primary_step_id else ""
        return f"{workflow_id}: {step}failed/degraded - {error.strip()[:160]}"
    if output.get("degraded"):
        step = f"{primary_step_id} " if isinstance(primary_step_id, str) and primary_step_id else ""
        return f"{workflow_id}: {step}degraded"
    message = output.get("message")
    if isinstance(message, str) and message.strip():
        return f"{workflow_id}: {message.strip()}"
    return f"{workflow_id}: workflow completed"


def _extract_analysis(value: Any) -> str | None:
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            text = value.strip()
            return text if len(text) > 50 else None
        return _extract_analysis(parsed)

    if not isinstance(value, dict):
        return None

    data = value.get("data")
    if isinstance(data, dict):
        analysis = _text_field(data)
        if analysis:
            return analysis

    analysis = _text_field(value)
    if analysis:
        return analysis

    output = value.get("output")
    if output is not None:
        return _extract_analysis(output)

    return None


def _text_field(value: dict[str, Any]) -> str | None:
    if value.get("error"):
        return None
    for key in ("analysis", "text", "content"):
        text = value.get(key)
        if isinstance(text, str) and len(text.strip()) > 50:
            return text.strip()
    return None


def _first_markdown_heading(text: str | None) -> str | None:
    if not text:
        return None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if set(line) <= {"-"}:
            continue
        if line.startswith("#"):
            line = line.lstrip("#").strip()
        return line[:120]
    return None
