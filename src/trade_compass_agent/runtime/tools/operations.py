from __future__ import annotations

import asyncio
import json
from datetime import date
from typing import Any

from trade_compass_agent.config import AppConfig
from trade_compass_agent.ops.job_definition import StepContext, StepOutput


_OPERATIONS: dict[str, str] = {
    "builtin.scan_portfolio_exits": "scan_portfolio_exits",
    "builtin.reconcile_portfolio_memory": "reconcile_portfolio_memory",
    "builtin.scan_overnight_news": "scan_overnight_news",
    "builtin.check_global_markets": "check_global_markets",
    "builtin.agent_premarket_briefing": "agent_premarket_briefing",
    "builtin.run_screening_engine": "run_screening_engine",
    "builtin.run_l5_screener": "run_l5_screener",
    "builtin.check_positions": "check_positions",
    "builtin.scan_sector_capital_flow": "scan_sector_capital_flow",
    "builtin.agent_morning_plan": "agent_morning_plan",
    "builtin.refresh_market_prices": "refresh_market_prices",
    "builtin.check_exit_signals": "check_exit_signals",
    "builtin.agent_close_analysis": "agent_close_analysis",
    "builtin.review_pnl": "review_pnl",
    "builtin.sync_instrument_pages": "sync_instrument_pages",
    "builtin.update_signal_tracker": "update_signal_tracker",
    "builtin.update_stock_profiles": "update_stock_profiles",
    "builtin.agent_eod_reflection": "agent_eod_reflection",
    "builtin.reflect_decisions": "reflect_decisions",
    "builtin.write_audit_summary": "write_audit_summary",
    "builtin.compact_memory": "compact_memory",
    "builtin.resolve_reflections": "resolve_reflections",
    "builtin.curate_knowledge": "curate_knowledge",
    "builtin.update_research_artifacts": "update_research_artifacts",
    "builtin.agent_daily_journal": "agent_daily_journal",
    "builtin.run_dreaming": "run_dreaming",
    "builtin.write_weekly_summary": "write_weekly_summary",
    "builtin.agent_strategy_review": "agent_strategy_review",
    "builtin.run_weekly_dreaming": "run_weekly_dreaming",
}


BUILTIN_OPERATION_TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": name,
            "description": f"Run the built-in Trade Compass operation {name}.",
            "parameters": {
                "type": "object",
                "properties": {
                    "job_id": {"type": "string"},
                    "as_of": {"type": "string"},
                    "run_id": {"type": "string"},
                    "step_timeout_seconds": {"type": "integer", "minimum": 1},
                    "upstream": {"type": "object"},
                },
                "required": ["job_id", "as_of"],
            },
        },
    }
    for name in sorted(_OPERATIONS)
]


def is_builtin_operation_tool(name: str) -> bool:
    return name in _OPERATIONS


def tool_run_builtin_operation(config: AppConfig, name: str, args: dict[str, Any]) -> str:
    handler_name = _OPERATIONS.get(name)
    if handler_name is None:
        return json.dumps({"error": f"unknown builtin operation tool: {name}"}, ensure_ascii=False)

    from trade_compass_agent.runtime.tools import builtin_operations as operation_handlers

    handler = getattr(operation_handlers, handler_name)
    ctx = StepContext(
        config=config,
        date=_parse_date(args.get("as_of")),
        upstream=_decode_upstream(args.get("upstream") or {}),
        step_timeout_seconds=_parse_optional_int(args.get("step_timeout_seconds")),
        job_id=str(args.get("job_id") or ""),
        run_id=str(args.get("run_id") or ""),
    )
    output = asyncio.run(handler(ctx))
    return json.dumps(
        {
            "message": output.message,
            "data": output.data,
        },
        ensure_ascii=False,
        default=str,
    )


def _parse_date(value: Any) -> date:
    if isinstance(value, date):
        return value
    text = str(value or "")
    try:
        return date.fromisoformat(text)
    except ValueError:
        return date.today()


def _parse_optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _decode_upstream(raw: Any) -> dict[str, StepOutput]:
    if not isinstance(raw, dict):
        return {}
    upstream: dict[str, StepOutput] = {}
    for key, value in raw.items():
        if isinstance(value, StepOutput):
            upstream[str(key)] = value
            continue
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                upstream[str(key)] = StepOutput(message=value, data={})
                continue
        if isinstance(value, dict):
            upstream[str(key)] = StepOutput(
                message=str(value.get("message") or ""),
                data=dict(value.get("data") if isinstance(value.get("data"), dict) else value),
            )
    return upstream
