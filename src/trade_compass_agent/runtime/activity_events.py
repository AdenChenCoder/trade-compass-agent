from __future__ import annotations

import json
import re
import time
from typing import Any

_SECRET_KEYS = re.compile(r"(api[_-]?key|token|password|secret|authorization)", re.I)


def parse_tool_arguments(arguments: str | dict | None) -> dict[str, Any]:
    if isinstance(arguments, dict):
        return dict(arguments)
    if isinstance(arguments, str) and arguments.strip():
        try:
            parsed = json.loads(arguments)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            return {}
    return {}


def redact_value(key: str, value: Any) -> Any:
    if _SECRET_KEYS.search(key):
        return "***"
    if isinstance(value, str) and len(value) > 120:
        return value[:117] + "..."
    return value


def redact_args(args: dict[str, Any]) -> dict[str, Any]:
    return {k: redact_value(k, v) for k, v in args.items()}


def tool_kind(name: str) -> str:
    if name == "load_skill":
        return "skill"
    if name.startswith("mcp_"):
        return "mcp"
    if name == "dispatch_specialists":
        return "specialist"
    return "tool"


def summarize_tool_label(name: str, args: dict[str, Any]) -> str:
    if name == "get_bars":
        symbol = args.get("symbol", "")
        timeframe = args.get("timeframe", "1d")
        return f"get_bars({symbol}, {timeframe})"
    if name == "get_market_pulse":
        return "get_market_pulse()"
    if name == "get_fundamentals":
        return f"get_fundamentals({args.get('symbol', '')})"
    if name == "get_events":
        symbol = args.get("symbol", "")
        limit = args.get("limit")
        if limit is not None:
            return f"get_events({symbol}, {limit})"
        return f"get_events({symbol})"
    if name == "fetch_url":
        return f"fetch_url({args.get('url', '')})"
    if name == "load_skill":
        return f"load_skill({args.get('name', '')})"
    if name == "dispatch_specialists":
        tasks = args.get("tasks") or []
        names: list[str] = []
        for task in tasks:
            if isinstance(task, dict):
                specialist = task.get("specialist") or task.get("name")
                if specialist:
                    names.append(str(specialist))
        if names:
            return f"dispatch_specialists({', '.join(names)})"
        return "dispatch_specialists()"
    if name == "search_memory":
        query = str(args.get("query", ""))[:48]
        return f'search_memory("{query}")'
    if name == "write_memory":
        return f"write_memory({args.get('scope', 'general')})"
    if name.startswith("mcp_"):
        remainder = name[4:]
        if "_" in remainder:
            server, tool = remainder.split("_", 1)
            return f"mcp:{server}/{tool}"
        return name
    if args:
        parts = [
            f"{key}={redact_value(key, value)}"
            for key, value in list(args.items())[:3]
        ]
        return f"{name}({', '.join(parts)})"
    return f"{name}()"


def tool_result_status(result: str) -> str:
    try:
        payload = json.loads(result)
    except json.JSONDecodeError:
        return "ok"
    if isinstance(payload, dict) and payload.get("error"):
        return "error"
    return "ok"


def build_tool_start_payload(name: str, arguments: str | dict | None) -> dict[str, Any]:
    args = parse_tool_arguments(arguments)
    return {
        "tool": name,
        "arguments": redact_args(args),
        "label": summarize_tool_label(name, args),
        "kind": tool_kind(name),
    }


def build_tool_end_payload(name: str, start_monotonic: float, result: str) -> dict[str, Any]:
    duration_ms = max(0, int((time.monotonic() - start_monotonic) * 1000))
    return {
        "tool": name,
        "preview": result[:500],
        "status": tool_result_status(result),
        "duration_ms": duration_ms,
        "kind": tool_kind(name),
        "label": summarize_tool_label(name, {}),
    }
