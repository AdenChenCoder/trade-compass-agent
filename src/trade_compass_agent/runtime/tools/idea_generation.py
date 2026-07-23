from __future__ import annotations

import hashlib
from datetime import date
import json
from typing import Any


IDEA_GENERATION_TOOL_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "build_idea_generation",
        "description": "Normalize A-share idea candidates with context and risk metadata.",
        "parameters": {
            "type": "object",
            "properties": {
                "as_of": {"type": "string"},
                "mode": {"type": "string"},
                "candidates": {"type": "array", "items": {"type": "object"}},
                "catalysts": {"type": "array", "items": {"type": "object"}},
                "market_pulse": {"type": "object"},
                "context": {"type": "object"},
                "risk_constraints": {"type": "object"},
                "source_refs": {"type": "array", "items": {"type": "string"}},
                "warnings": {"type": "array", "items": {"type": "string"}},
                "run_id": {"type": "string"},
            },
            "required": ["as_of", "mode"],
        },
    },
}


class _ToolManifest:
    id = "idea_generation_cn"
    version = 2


def tool_build_idea_generation(**args: Any) -> str:
    output = build_idea_generation(
        inputs=args,
        manifest=_ToolManifest(),
    )
    return json.dumps(output, ensure_ascii=False, sort_keys=True)


def build_idea_generation(*, inputs, manifest) -> dict[str, Any]:
    as_of = str(inputs.get("as_of") or date.today().isoformat())
    mode = str(inputs.get("mode") or "manual")
    source_candidates = inputs.get("candidates") or []
    context = inputs.get("context") if isinstance(inputs.get("context"), dict) else {}
    catalysts = inputs.get("catalysts") if isinstance(inputs.get("catalysts"), list) else []
    market_pulse = inputs.get("market_pulse") if isinstance(inputs.get("market_pulse"), dict) else {}
    risk_constraints = inputs.get("risk_constraints") if isinstance(inputs.get("risk_constraints"), dict) else {}
    source_refs = [str(x) for x in inputs.get("source_refs") or [] if str(x).strip()]
    context = {
        **context,
        "market_pulse": context.get("market_pulse") or market_pulse,
        "risk_constraints": context.get("risk_constraints") or risk_constraints,
        "catalysts": context.get("catalysts") or catalysts,
    }
    ideas: list[dict[str, Any]] = []
    for raw in source_candidates:
        if not isinstance(raw, dict):
            continue
        symbol = str(raw.get("symbol") or "").strip()
        if not symbol:
            continue
        drivers = [str(x) for x in raw.get("drivers") or raw.get("reasons") or [] if str(x).strip()]
        if not drivers:
            drivers = ["entered workflow without explicit driver; requires research validation"]
        score = _coerce_score(raw.get("score"), drivers, raw.get("risks") or [])
        ideas.append(
            {
                "idea_id": _id("idea", as_of, symbol, str(raw.get("theme") or "")),
                "as_of": as_of,
                "symbol": symbol,
                "name": str(raw.get("name") or ""),
                "sector": str(raw.get("sector") or ""),
                "theme": str(raw.get("theme") or ""),
                "direction": _enum(
                    raw.get("direction"),
                    {"watch", "research", "avoid", "risk_check"},
                    "watch",
                ),
                "score": max(0, min(score, 100)),
                "score_components": dict(raw.get("score_components") or {}),
                "drivers": drivers,
                "risks": [str(x) for x in raw.get("risks") or []],
                "invalidation_conditions": [
                    str(x) for x in raw.get("invalidation_conditions") or []
                ],
                "next_step": str(raw.get("next_step") or "run equity_research before any action"),
                "related_catalyst_ids": [str(x) for x in raw.get("related_catalyst_ids") or []],
                "related_prior_idea_ids": [str(x) for x in raw.get("related_prior_idea_ids") or []],
                "source_refs": [str(x) for x in raw.get("source_refs") or source_refs],
                "no_trade_disclaimer": True,
            }
        )
    return {
        "workflow_id": manifest.id,
        "workflow_version": manifest.version,
        "run_id": str(inputs.get("run_id") or ""),
        "as_of": as_of,
        "mode": mode if mode in {"morning", "weekend", "manual"} else "manual",
        "context": context,
        "ideas": ideas,
        "warnings": [str(x) for x in inputs.get("warnings") or []],
    }


def _score_from_drivers(drivers: list[str], risks: list[str]) -> int:
    return max(0, min(100, 50 + len(drivers) * 8 - len(risks) * 6))


def _coerce_score(value, drivers: list[str], risks: list[str]) -> int:
    if value is None or value == "":
        return _score_from_drivers(drivers, risks)
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return _score_from_drivers(drivers, risks)
    return int(numeric * 100) if numeric <= 1 else int(numeric)


def _id(*parts: str) -> str:
    payload = "|".join(parts)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def _enum(value, allowed: set[str], default: str) -> str:
    text = str(value or "").strip()
    return text if text in allowed else default
