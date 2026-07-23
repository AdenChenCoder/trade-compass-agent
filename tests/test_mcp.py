from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from trade_compass_agent.runtime.mcp.client import (
    McpClientRegistry,
    endpoint_display,
    infer_transport,
    make_mcp_tool_name,
    mcp_config_paths,
    merge_mcp_server_specs,
    parse_mcp_config_paths,
    redact_url,
)
from trade_compass_agent.runtime.mcp.loader import load_mcp_config


@pytest.fixture()
def mcp_fixture_json(tmp_path: Path) -> Path:
    path = tmp_path / "mcp.json"
    path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "demo": {
                        "command": "echo",
                        "args": ["mcp"],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    return path


@pytest.fixture()
def mcp_http_fixture_json(tmp_path: Path) -> Path:
    path = tmp_path / "mcp.json"
    path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "remote": {
                        "url": "https://api.example.com/mcp/?token=secret",
                        "transport": "http",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    return path


def test_parse_mcp_config_paths(mcp_fixture_json: Path) -> None:
    servers = parse_mcp_config_paths(mcp_fixture_json)
    assert "demo" in servers
    assert servers["demo"]["command"] == "echo"


def test_parse_mcp_config_paths_url(mcp_http_fixture_json: Path) -> None:
    servers = parse_mcp_config_paths(mcp_http_fixture_json)
    assert "remote" in servers
    assert servers["remote"]["url"].startswith("https://")


def test_mcp_user_config_follows_trade_compass_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TRADE_COMPASS_HOME", str(tmp_path / "custom-home"))

    assert mcp_config_paths()[-1] == tmp_path / "custom-home" / "mcp.json"


def test_make_mcp_tool_name() -> None:
    assert make_mcp_tool_name("My Server", "List Files") == "mcp_my_server_list_files"


def test_infer_transport() -> None:
    assert infer_transport({"command": "echo", "args": []}) == "stdio"
    assert infer_transport({"url": "https://example.com/mcp"}) == "http"
    assert infer_transport({"url": "https://example.com/mcp", "transport": "sse"}) == "sse"


def test_redact_url() -> None:
    assert redact_url("https://api.example.com/mcp") == "https://api.example.com/mcp"
    assert redact_url("https://api.example.com/mcp?token=secret") == "https://api.example.com/mcp?***"


def test_endpoint_display_redacts_query() -> None:
    display = endpoint_display({"url": "https://api.example.com/mcp?token=secret"})
    assert display == "https://api.example.com/mcp?***"


def test_endpoint_display_env_substitution(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXAMPLE_TOKEN", "abc123")
    display = endpoint_display({"url": "https://api.example.com/mcp?token=${EXAMPLE_TOKEN}"})
    assert display == "https://api.example.com/mcp?***"


@patch("trade_compass_agent.runtime.mcp.client.merge_mcp_server_specs")
@patch("trade_compass_agent.runtime.mcp.client._probe_server", new_callable=AsyncMock)
def test_registry_probe_connected(mock_probe, mock_merge) -> None:
    from trade_compass_agent.runtime.mcp.client import McpServerConnection, McpToolSpec

    mock_merge.return_value = {"demo": {"command": "echo", "args": []}}
    mock_probe.return_value = McpServerConnection(
        name="demo",
        status="connected",
        transport="stdio",
        command="echo mcp",
        tools=["ping"],
        tool_specs=[
            McpToolSpec(
                server_name="demo",
                remote_name="ping",
                local_name="mcp_demo_ping",
                description="ping tool",
                parameters={"type": "object", "properties": {}},
            )
        ],
    )

    registry = McpClientRegistry()
    servers = registry.probe(force=True)
    assert servers[0].status == "connected"
    assert servers[0].transport == "stdio"
    assert registry.tool_schemas[0]["function"]["name"] == "mcp_demo_ping"


def test_registry_probe_http() -> None:
    from trade_compass_agent.runtime.mcp.client import _probe_server

    tool = MagicMock()
    tool.name = "fetch"
    tool.description = "fetch data"
    tool.inputSchema = {"type": "object", "properties": {}}

    listed = MagicMock()
    listed.tools = [tool]

    session = AsyncMock()
    session.list_tools.return_value = listed

    @asynccontextmanager
    async def fake_session(*_args, **_kwargs):
        yield session

    import asyncio

    with (
        patch("trade_compass_agent.runtime.mcp.client._mcp_available", return_value=True),
        patch("trade_compass_agent.runtime.mcp.client._mcp_session", fake_session),
    ):
        conn = asyncio.run(
            _probe_server(
                "remote",
                {"url": "https://api.example.com/mcp", "transport": "http"},
            )
        )
    assert conn.status == "connected"
    assert conn.transport == "http"
    assert conn.tools == ["fetch"]
    assert conn.command == "https://api.example.com/mcp"
    assert conn.tool_specs[0].local_name == "mcp_remote_fetch"


def test_registry_probe_sse() -> None:
    from trade_compass_agent.runtime.mcp.client import _probe_server

    tool = MagicMock()
    tool.name = "ping"
    tool.description = None
    tool.inputSchema = None

    listed = MagicMock()
    listed.tools = [tool]

    session = AsyncMock()
    session.list_tools.return_value = listed

    @asynccontextmanager
    async def fake_session(*_args, **_kwargs):
        yield session

    import asyncio

    with (
        patch("trade_compass_agent.runtime.mcp.client._mcp_available", return_value=True),
        patch("trade_compass_agent.runtime.mcp.client._mcp_session", fake_session),
    ):
        conn = asyncio.run(
            _probe_server(
                "remote",
                {"url": "https://api.example.com/sse", "transport": "sse"},
            )
        )
    assert conn.status == "connected"
    assert conn.transport == "sse"


def test_load_mcp_config_static(mcp_fixture_json: Path) -> None:
    statuses = load_mcp_config(mcp_fixture_json)
    assert statuses[0].name == "demo"
    assert statuses[0].status == "configured"
    assert statuses[0].transport == "stdio"


def test_load_mcp_config_static_url(mcp_http_fixture_json: Path) -> None:
    statuses = load_mcp_config(mcp_http_fixture_json)
    assert statuses[0].name == "remote"
    assert statuses[0].transport == "http"
    assert statuses[0].command == "https://api.example.com/mcp/?***"


def test_merge_paths_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = tmp_path / "project" / ".trade-compass" / "mcp.json"
    user = tmp_path / "user" / ".trade-compass" / "mcp.json"
    project.parent.mkdir(parents=True)
    user.parent.mkdir(parents=True)
    project.write_text(
        json.dumps({"mcpServers": {"a": {"command": "one"}}}),
        encoding="utf-8",
    )
    user.write_text(
        json.dumps({"mcpServers": {"a": {"command": "two"}, "b": {"command": "three"}}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "trade_compass_agent.runtime.mcp.client.mcp_config_paths",
        lambda: [project, user],
    )
    merged = merge_mcp_server_specs()
    assert merged["a"]["command"] == "two"
    assert "b" in merged
