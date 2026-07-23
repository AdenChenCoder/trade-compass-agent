from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

@dataclass(frozen=True)
class McpServerStatus:
    name: str
    status: str
    transport: str | None = None
    command: str | None = None
    tools: list[str] | None = None
    error: str | None = None


def parse_mcp_config_paths(path: Path) -> dict[str, dict[str, Any]]:
    """Parse one ``mcp.json`` file containing an ``mcpServers`` object."""
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    servers = raw.get("mcpServers") or raw.get("servers") or {}
    result: dict[str, dict[str, Any]] = {}
    for name, spec in servers.items():
        if isinstance(spec, dict):
            result[str(name)] = spec
    return result


def load_mcp_config(path: Path | None = None) -> list[McpServerStatus]:
    """Return live MCP server status (connected / error / configured)."""
    if path is not None:
        from trade_compass_agent.runtime.mcp.client import endpoint_display, infer_transport

        servers = parse_mcp_config_paths(path)
        return [
            McpServerStatus(
                name=name,
                status="configured",
                transport=infer_transport(spec),
                command=endpoint_display(spec),
                tools=[],
            )
            for name, spec in servers.items()
        ]

    from trade_compass_agent.runtime.mcp.client import get_mcp_registry

    return get_mcp_registry().status_list()
