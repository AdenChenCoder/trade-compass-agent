from __future__ import annotations

import json
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any

from trade_compass_agent.config import resolve_schema_path
from trade_compass_agent.runtime.readers import ReaderInput, ReaderType, read_untrusted_text
from trade_compass_agent.runtime.schema_validator import SchemaValidationError, validate_schema

READER_TOOL_TYPES: dict[str, ReaderType] = {
    "read_announcements": "announcement_reader",
    "read_news": "news_reader",
    "read_research_report": "research_report_reader",
    "read_kol_signal": "kol_signal_reader",
    "read_webpage": "webpage_reader",
}

READER_TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": name,
            "description": f"Validate untrusted external content as {reader_type} evidence.",
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {"type": "string"},
                    "source": {"type": "string"},
                    "source_url": {"type": "string"},
                    "source_title": {"type": "string"},
                    "published_at": {"type": "string"},
                    "symbols": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["content", "source"],
            },
        },
    }
    for name, reader_type in READER_TOOL_TYPES.items()
]

_READER_OUTPUT_SCHEMA_PATH = resolve_schema_path("readers/reader_claims.schema.json")
_MAX_WARNING_LENGTH = 300


def run_reader_tool(name: str, **kwargs: Any) -> str:
    reader_type = READER_TOOL_TYPES.get(name)
    if reader_type is None:
        return json.dumps({"error": f"unknown reader tool: {name}"}, ensure_ascii=False)
    symbols = kwargs.get("symbols") or ()
    if isinstance(symbols, str):
        symbols = tuple(item.strip() for item in symbols.split(",") if item.strip())
    source = str(kwargs.get("source") or "")
    source_url = str(kwargs.get("source_url") or "")
    result = read_untrusted_text(
        ReaderInput(
            reader_type=reader_type,
            content=str(kwargs.get("content") or ""),
            source=source,
            source_url=source_url,
            source_title=str(kwargs.get("source_title") or ""),
            published_at=str(kwargs.get("published_at") or ""),
            symbols=tuple(str(item) for item in symbols),
        )
    ).model_dump()
    result = _validate_or_degrade_reader_result(result)
    return json.dumps(result, ensure_ascii=False, sort_keys=True)


def _validate_or_degrade_reader_result(result: dict[str, Any]) -> dict[str, Any]:
    schema = json.loads(_READER_OUTPUT_SCHEMA_PATH.read_text(encoding="utf-8"))
    try:
        validate_schema(result, schema)
        return result
    except SchemaValidationError as exc:
        degraded = _coerce_degraded_reader_result(result, f"reader schema validation failed: {exc}")
        validate_schema(degraded, schema)
        return degraded


def _coerce_degraded_reader_result(result: dict[str, Any], warning: str) -> dict[str, Any]:
    degraded = deepcopy(result)
    now = datetime.now(UTC).isoformat()
    degraded["schema_version"] = int(degraded.get("schema_version") or 1)
    degraded["as_of"] = _bounded_text(degraded.get("as_of"), 64) or now
    degraded["reader_type"] = str(degraded.get("reader_type") or "webpage_reader")
    degraded["source"] = _coerce_source(degraded.get("source"), now)
    degraded["source_refs"] = [
        _bounded_text(item, 1000) or "unknown-source"
        for item in _coerce_list(degraded.get("source_refs"))
    ][:50]
    if not degraded["source_refs"]:
        degraded["source_refs"] = [degraded["source"]["source"]]
    degraded["symbols"] = [_bounded_text(item, 16) for item in _coerce_list(degraded.get("symbols"))]
    degraded["entities"] = [_bounded_text(item, 80) for item in _coerce_list(degraded.get("entities"))]
    degraded["claims"] = []
    degraded["events"] = []
    degraded["risks"] = [_bounded_text(item, 500) for item in _coerce_list(degraded.get("risks"))]
    degraded["unsupported_claims"] = [
        _bounded_text(item, 500) for item in _coerce_list(degraded.get("unsupported_claims"))
    ]
    degraded["confidence"] = "low"
    degraded["validation_status"] = "degraded"
    warnings = [_bounded_text(item, _MAX_WARNING_LENGTH) for item in _coerce_list(degraded.get("warnings"))]
    warnings.append(_bounded_text(warning, _MAX_WARNING_LENGTH))
    degraded["warnings"] = warnings
    degraded["trace_events"] = _coerce_trace_events(degraded.get("trace_events"), warning)
    return degraded


def _coerce_source(value: Any, now: str) -> dict[str, str]:
    source = value if isinstance(value, dict) else {}
    return {
        "source": _bounded_text(source.get("source"), 256) or "unknown-source",
        "source_url": _bounded_text(source.get("source_url"), 1000),
        "source_title": _bounded_text(source.get("source_title"), 300),
        "published_at": _bounded_text(source.get("published_at"), 64),
        "retrieved_at": _bounded_text(source.get("retrieved_at"), 64) or now,
    }


def _coerce_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return []


def _bounded_text(value: Any, max_length: int) -> str:
    return str(value or "")[:max_length]


def _coerce_trace_events(value: Any, warning: str) -> list[dict[str, Any]]:
    events = [item for item in _coerce_list(value) if isinstance(item, dict)]
    events.append(
        {
            "event": "reader.schema_degraded",
            "warning": _bounded_text(warning, _MAX_WARNING_LENGTH),
        }
    )
    return events
