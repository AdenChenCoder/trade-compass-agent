from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import threading
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse

from trade_compass_agent.config import PROJECT_ROOT, user_home_path
from trade_compass_agent.runtime.mcp.loader import McpServerStatus, parse_mcp_config_paths

_NAME_SEGMENT_RE = re.compile(r"[^a-z0-9]+")
_ENV_VAR_RE = re.compile(r"\$\{([^}]+)\}")

logger = logging.getLogger(__name__)


@dataclass
class McpToolSpec:
    server_name: str
    remote_name: str
    local_name: str
    description: str
    parameters: dict[str, Any]


@dataclass
class McpServerConnection:
    name: str
    status: str
    transport: str | None = None
    command: str | None = None
    tools: list[str] = field(default_factory=list)
    error: str | None = None
    tool_specs: list[McpToolSpec] = field(default_factory=list)


def mcp_config_paths() -> list[Path]:
    """Project and user MCP config locations using an ``mcpServers`` object."""
    home = user_home_path() / "mcp.json"
    project = PROJECT_ROOT / ".trade-compass" / "mcp.json"
    return [project, home]


def merge_mcp_server_specs() -> dict[str, dict[str, Any]]:
    """Merge server specs; later files override same server name."""
    merged: dict[str, dict[str, Any]] = {}
    for path in mcp_config_paths():
        for name, spec in parse_mcp_config_paths(path).items():
            merged[name] = spec
    return merged


def make_mcp_tool_name(server_name: str, tool_name: str) -> str:
    server = _sanitize_segment(server_name)
    tool = _sanitize_segment(tool_name)
    return f"mcp_{server}_{tool}"


def _sanitize_segment(value: str) -> str:
    normalized = _NAME_SEGMENT_RE.sub("_", value.strip().lower()).strip("_")
    return normalized or "tool"


def _substitute_env(value: str) -> str:
    def _replace(match: re.Match[str]) -> str:
        key = match.group(1)
        return os.environ.get(key, match.group(0))

    return _ENV_VAR_RE.sub(_replace, value)


def _resolve_url(url: str) -> str:
    return _substitute_env(str(url))


def redact_url(url: str) -> str:
    """Return URL safe for logs and API responses (query string redacted)."""
    parsed = urlparse(url)
    if not parsed.query:
        return url
    return urlunparse(parsed._replace(query="***"))


def _resolve_headers(spec: dict[str, Any]) -> dict[str, str]:
    raw = spec.get("headers") or {}
    return {str(k): _substitute_env(str(v)) for k, v in raw.items()}


def _normalize_transport(value: str | None) -> str | None:
    if not value:
        return None
    normalized = str(value).strip().lower().replace("_", "-")
    if normalized in {"stdio", "sse"}:
        return normalized
    if normalized in {"http", "streamable-http", "streamablehttp"}:
        return "http"
    return normalized


def infer_transport(spec: dict[str, Any]) -> str | None:
    if spec.get("command"):
        return "stdio"
    if spec.get("url"):
        return _normalize_transport(spec.get("transport")) or "http"
    return None


def endpoint_display(spec: dict[str, Any]) -> str | None:
    command = spec.get("command")
    if command:
        args = spec.get("args") or []
        return " ".join([str(command), *[str(a) for a in args]])
    url = spec.get("url")
    if url:
        return redact_url(_resolve_url(str(url)))
    return None


def _normalize_schema(schema: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(schema, dict):
        return {"type": "object", "properties": {}, "required": []}
    result = dict(schema)
    if result.get("type") != "object":
        result["type"] = "object"
    if "properties" not in result:
        result["properties"] = {}
    if "required" not in result:
        result["required"] = []
    return result


def _run_sync(coro_factory):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro_factory())

    result: dict[str, Any] = {}
    failure: dict[str, BaseException] = {}

    def _runner() -> None:
        try:
            result["value"] = asyncio.run(coro_factory())
        except BaseException as exc:
            failure["error"] = exc

    thread = threading.Thread(target=_runner, daemon=True)
    thread.start()
    thread.join()
    if "error" in failure:
        raise failure["error"]
    return result["value"]


def _mcp_available() -> bool:
    try:
        import mcp  # noqa: F401
        return True
    except ImportError:
        return False


@asynccontextmanager
async def _mcp_session(spec: dict[str, Any], *, transport: str | None = None):
    from mcp import ClientSession

    resolved_transport = transport or infer_transport(spec)
    if resolved_transport == "stdio":
        from mcp.client.stdio import StdioServerParameters, stdio_client

        env = os.environ.copy()
        env.update({str(k): str(v) for k, v in (spec.get("env") or {}).items()})
        params = StdioServerParameters(
            command=str(spec["command"]),
            args=[str(a) for a in (spec.get("args") or [])],
            env=env,
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session
        return

    url = _resolve_url(str(spec["url"]))
    headers = _resolve_headers(spec)
    safe_url = redact_url(url)
    if resolved_transport == "sse":
        from mcp.client.sse import sse_client

        logger.debug("Connecting MCP SSE server at %s", safe_url)
        async with sse_client(url, headers=headers or None) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session
        return

    if resolved_transport == "http":
        from mcp.client.streamable_http import streamable_http_client
        from mcp.shared._httpx_utils import create_mcp_http_client

        logger.debug("Connecting MCP HTTP server at %s", safe_url)
        client = create_mcp_http_client(headers=headers or None)
        async with client:
            async with streamable_http_client(url, http_client=client) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    yield session
        return

    raise ValueError(f"unsupported MCP transport: {resolved_transport}")


async def _collect_tool_specs(name: str, session) -> tuple[list[Any], list[McpToolSpec]]:
    listed = await session.list_tools()
    specs: list[McpToolSpec] = []
    seen: dict[str, str] = {}
    for tool in listed.tools:
        local = _dedupe_tool_name(
            make_mcp_tool_name(name, tool.name),
            tool.name,
            seen,
        )
        specs.append(
            McpToolSpec(
                server_name=name,
                remote_name=tool.name,
                local_name=local,
                description=tool.description or f"MCP tool {tool.name} from {name}",
                parameters=_normalize_schema(getattr(tool, "inputSchema", None)),
            )
        )
    return listed.tools, specs


async def _list_remote_tools(
    name: str,
    spec: dict[str, Any],
    *,
    transport: str | None = None,
) -> tuple[list[Any], list[McpToolSpec], str]:
    explicit = _normalize_transport(spec.get("transport"))
    if spec.get("command"):
        async with _mcp_session(spec, transport="stdio") as session:
            tools, tool_specs = await _collect_tool_specs(name, session)
        return tools, tool_specs, "stdio"

    if explicit == "sse":
        async with _mcp_session(spec, transport="sse") as session:
            tools, tool_specs = await _collect_tool_specs(name, session)
        return tools, tool_specs, "sse"

    if explicit == "http":
        async with _mcp_session(spec, transport="http") as session:
            tools, tool_specs = await _collect_tool_specs(name, session)
        return tools, tool_specs, "http"

    last_error: Exception | None = None
    for candidate in ("http", "sse"):
        try:
            async with _mcp_session(spec, transport=candidate) as session:
                tools, tool_specs = await _collect_tool_specs(name, session)
            return tools, tool_specs, candidate
        except Exception as exc:
            last_error = exc
    if last_error is not None:
        raise last_error
    raise RuntimeError("no MCP transport available")


async def _probe_server(name: str, spec: dict[str, Any]) -> McpServerConnection:
    display = endpoint_display(spec)
    transport = infer_transport(spec)

    if not _mcp_available():
        return McpServerConnection(
            name=name,
            status="unavailable",
            transport=transport,
            command=display,
            error="mcp package not installed; pip install -e '.[mcp]'",
        )

    if not spec.get("command") and not spec.get("url"):
        return McpServerConnection(
            name=name,
            status="error",
            transport=transport,
            command=display,
            error="missing command or url in mcp.json",
        )

    try:
        tools, specs, used_transport = await _list_remote_tools(name, spec)
        return McpServerConnection(
            name=name,
            status="connected",
            transport=used_transport,
            command=display,
            tools=[t.name for t in tools],
            tool_specs=specs,
        )
    except Exception as exc:
        logger.debug("MCP probe failed for %s: %s", name, exc)
        return McpServerConnection(
            name=name,
            status="error",
            transport=transport,
            command=display,
            error=str(exc),
        )


async def _call_remote_tool(
    server_name: str,
    spec: dict[str, Any],
    remote_name: str,
    arguments: dict[str, Any],
    *,
    transport: str | None = None,
) -> dict[str, Any]:
    async with _mcp_session(spec, transport=transport) as session:
        result = await session.call_tool(remote_name, arguments)
        return _normalize_tool_result(result)


def _normalize_tool_result(result: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"status": "ok"}
    if getattr(result, "isError", False):
        payload["status"] = "error"
    content = getattr(result, "content", None) or []
    texts: list[str] = []
    for block in content:
        text = getattr(block, "text", None)
        if text:
            texts.append(str(text))
    if texts:
        payload["text"] = "\n".join(texts)
    structured = getattr(result, "structuredContent", None)
    if structured is not None:
        payload["structured_content"] = structured
    return payload


def _dedupe_tool_name(candidate: str, remote_name: str, seen: dict[str, str]) -> str:
    existing = seen.get(candidate)
    if existing is None:
        seen[candidate] = remote_name
        return candidate
    if existing == remote_name:
        return candidate
    suffix = hashlib.sha1(remote_name.encode()).hexdigest()[:8]
    unique = f"{candidate}_{suffix}"
    seen[unique] = remote_name
    return unique


class McpClientRegistry:
    """Probe MCP servers and expose tool schemas for the agent loop."""

    def __init__(self) -> None:
        self._servers: list[McpServerConnection] = []
        self._specs: dict[str, McpToolSpec] = {}
        self._server_specs: dict[str, dict[str, Any]] = {}
        self._server_transports: dict[str, str] = {}
        self._probed = False

    def probe(self, *, force: bool = False) -> list[McpServerConnection]:
        if self._probed and not force:
            return self._servers
        self._server_specs = merge_mcp_server_specs()
        if not self._server_specs:
            self._servers = []
            self._specs = {}
            self._server_transports = {}
            self._probed = True
            return self._servers

        async def _probe_all():
            results = []
            for name, spec in self._server_specs.items():
                results.append(await _probe_server(name, spec))
            return results

        try:
            self._servers = _run_sync(_probe_all)
        except Exception as exc:
            logger.warning("MCP probe aborted: %s", exc)
            self._servers = [
                McpServerConnection(
                    name=name,
                    status="error",
                    transport=infer_transport(spec),
                    command=endpoint_display(spec),
                    error=str(exc),
                )
                for name, spec in self._server_specs.items()
            ]
        self._specs = {}
        self._server_transports = {}
        for conn in self._servers:
            if conn.transport:
                self._server_transports[conn.name] = conn.transport
            for spec in conn.tool_specs:
                self._specs[spec.local_name] = spec
        self._probed = True
        return self._servers

    def status_list(self) -> list[McpServerStatus]:
        servers = self.probe()
        return [
            McpServerStatus(
                name=s.name,
                status=s.status,
                transport=s.transport,
                command=s.command,
                tools=s.tools or [],
                error=s.error,
            )
            for s in servers
        ]

    @property
    def tool_schemas(self) -> list[dict[str, Any]]:
        self.probe()
        schemas: list[dict[str, Any]] = []
        for spec in self._specs.values():
            schemas.append(
                {
                    "type": "function",
                    "function": {
                        "name": spec.local_name,
                        "description": spec.description,
                        "parameters": spec.parameters,
                    },
                }
            )
        return schemas

    def execute(self, local_name: str, arguments: dict[str, Any]) -> str:
        self.probe()
        spec = self._specs.get(local_name)
        if spec is None:
            return json.dumps({"error": f"unknown MCP tool: {local_name}"}, ensure_ascii=False)
        server_spec = self._server_specs.get(spec.server_name)
        if server_spec is None:
            return json.dumps({"error": f"MCP server not configured: {spec.server_name}"}, ensure_ascii=False)

        transport = self._server_transports.get(spec.server_name)

        async def _invoke():
            return await _call_remote_tool(
                spec.server_name,
                server_spec,
                spec.remote_name,
                arguments,
                transport=transport,
            )

        try:
            payload = _run_sync(_invoke)
            payload["server"] = spec.server_name
            payload["remote_tool"] = spec.remote_name
            payload["tool"] = local_name
            return json.dumps(payload, ensure_ascii=False)
        except Exception as exc:
            return json.dumps(
                {
                    "status": "error",
                    "server": spec.server_name,
                    "remote_tool": spec.remote_name,
                    "tool": local_name,
                    "error": str(exc),
                },
                ensure_ascii=False,
            )

    def is_mcp_tool(self, name: str) -> bool:
        self.probe()
        return name in self._specs


_registry: McpClientRegistry | None = None


def get_mcp_registry() -> McpClientRegistry:
    global _registry
    if _registry is None:
        _registry = McpClientRegistry()
    return _registry
