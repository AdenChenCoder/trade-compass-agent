from __future__ import annotations

import hashlib
from datetime import date
import json
from typing import Any


CATALYST_CALENDAR_TOOL_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "build_catalyst_calendar",
        "description": "Normalize validated catalyst inputs into A-share catalyst calendar events.",
        "parameters": {
            "type": "object",
            "properties": {
                "as_of": {"type": "string"},
                "horizon_days": {"type": "integer", "default": 14},
                "events": {"type": "array", "items": {"type": "object"}},
                "reader_results": {"type": "array", "items": {"type": ["object", "string"]}},
                "symbols": {"type": "array", "items": {"type": "string"}},
                "sectors": {"type": "array", "items": {"type": "string"}},
                "source_refs": {"type": "array", "items": {"type": "string"}},
                "warnings": {"type": "array", "items": {"type": "string"}},
                "run_id": {"type": "string"},
            },
            "required": ["as_of", "horizon_days"],
        },
    },
}


class _ToolManifest:
    id = "catalyst_calendar_cn"
    version = 2


def tool_build_catalyst_calendar(**args: Any) -> str:
    output = build_catalyst_calendar(
        inputs=args,
        manifest=_ToolManifest(),
    )
    return json.dumps(output, ensure_ascii=False, sort_keys=True)


def build_catalyst_calendar(*, inputs, manifest) -> dict[str, Any]:
    as_of = str(inputs.get("as_of") or date.today().isoformat())
    horizon_days = int(inputs.get("horizon_days") or 14)
    source_events = inputs.get("events") or []
    reader_results = _reader_results(inputs.get("reader_results"))
    input_symbols = [str(x) for x in inputs.get("symbols") or [] if str(x).strip()]
    input_sectors = [str(x) for x in inputs.get("sectors") or [] if str(x).strip()]
    input_source_refs = [str(x) for x in inputs.get("source_refs") or [] if str(x).strip()]
    reader_source_refs = [
        str(ref)
        for reader in reader_results
        for ref in reader.get("source_refs") or []
        if str(ref).strip()
    ]
    reader_warnings = [
        str(warning)
        for reader in reader_results
        for warning in reader.get("warnings") or []
        if str(warning).strip()
    ]
    source_events = [*source_events, *_reader_events(reader_results)]
    events: list[dict[str, Any]] = []
    for raw in source_events:
        if not isinstance(raw, dict):
            continue
        summary = str(raw.get("summary") or raw.get("claim") or "").strip()
        if not summary:
            continue
        symbol = str(raw.get("symbol") or _first(raw.get("symbols")) or "")
        event_type = str(raw.get("event_type") or "unknown")
        source_refs = (
            raw.get("source_refs")
            or input_source_refs
            or reader_source_refs
            or [str(raw.get("source") or "validated reader output")]
        )
        events.append(
            {
                "event_id": _id("catalyst", as_of, symbol, event_type, summary),
                "symbol": symbol,
                "name": str(raw.get("name") or ""),
                "sector": str(raw.get("sector") or ""),
                "event_date": str(raw.get("event_date") or ""),
                "event_type": event_type,
                "summary": summary[:800],
                "expected_impact": _enum(
                    raw.get("expected_impact"),
                    {"low", "medium", "high", "unknown"},
                    "unknown",
                ),
                "uncertainty": str(raw.get("uncertainty") or ""),
                "confidence": _enum(raw.get("confidence"), {"low", "medium", "high"}, "medium"),
                "related_symbols": [str(x) for x in raw.get("related_symbols") or raw.get("symbols") or []],
                "related_holdings": [str(x) for x in raw.get("related_holdings") or []],
                "related_idea_ids": [str(x) for x in raw.get("related_idea_ids") or []],
                "source_refs": [str(x) for x in source_refs if str(x).strip()] or ["validated reader output"],
                "stale_status": _enum(
                    raw.get("stale_status"),
                    {"active", "stale", "archived"},
                    "active",
                ),
                "suggested_workflow_action": _enum(
                    raw.get("suggested_workflow_action"),
                    {"watch", "risk_check", "research", "archive"},
                    "watch",
                ),
                "no_trade_disclaimer": True,
            }
        )
    return {
        "workflow_id": manifest.id,
        "workflow_version": manifest.version,
        "run_id": str(inputs.get("run_id") or ""),
        "as_of": as_of,
        "horizon_days": horizon_days,
        "symbols": input_symbols,
        "sectors": input_sectors,
        "events": events,
        "warnings": list(dict.fromkeys([*[str(x) for x in inputs.get("warnings") or []], *reader_warnings])),
    }


def _first(value) -> str:
    if isinstance(value, list | tuple) and value:
        return str(value[0])
    return ""


def _reader_results(value: Any) -> list[dict[str, Any]]:
    values = value if isinstance(value, list | tuple) else [value]
    results: list[dict[str, Any]] = []
    for item in values:
        if isinstance(item, dict):
            if item.get("skipped"):
                continue
            results.append(item)
            continue
        if isinstance(item, str) and item.strip():
            try:
                parsed = json.loads(item)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict) and not parsed.get("skipped"):
                results.append(parsed)
    return results


def _reader_events(readers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for reader in readers:
        for event in reader.get("events") or []:
            if not isinstance(event, dict):
                continue
            events.append(
                {
                    **event,
                    "source_refs": reader.get("source_refs") or [],
                    "source": (reader.get("source") or {}).get("source", ""),
                }
            )
    return events


def _id(*parts: str) -> str:
    payload = "|".join(parts)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def _enum(value, allowed: set[str], default: str) -> str:
    text = str(value or "").strip()
    return text if text in allowed else default
