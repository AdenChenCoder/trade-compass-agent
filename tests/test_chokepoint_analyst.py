"""Unit tests for search_x_kol tool and chokepoint_analyst specialist."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from trade_compass_agent.config import load_app_config
from trade_compass_agent.llm.providers import ChatCompletion, ToolCall
from trade_compass_agent.runtime.market_stack import MarketStack
from trade_compass_agent.runtime.specialists.asset_runner import run_asset_specialist
from trade_compass_agent.runtime.specialists.assets import load_specialist_profiles


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def specialist_stack(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> MarketStack:
    monkeypatch.setenv("TRADE_COMPASS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("TRADE_COMPASS_MEMORY_DIR", str(tmp_path / "memory"))
    monkeypatch.setenv("TRADE_COMPASS_DATA_PROVIDER", "sample")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    src_skills = Path(__file__).resolve().parents[1] / "memory_vault" / "skills"
    dest_skills = tmp_path / "memory" / "skills"
    if src_skills.is_dir():
        shutil.copytree(src_skills, dest_skills)

    config = load_app_config()
    return MarketStack.from_config(config)


# ---------------------------------------------------------------------------
# search_x_kol tool tests
# ---------------------------------------------------------------------------

class TestSearchXKol:
    """Tests for tool_search_x_kol."""

    def test_missing_api_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("XAI_API_KEY", raising=False)
        from trade_compass_agent.runtime.tools.search import tool_search_x_kol

        result = json.loads(tool_search_x_kol())
        assert "error" in result
        assert "XAI_API_KEY" in result["error"]

    def test_default_handles(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("XAI_API_KEY", "test-key")

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "output": [
                {
                    "type": "message",
                    "content": [
                        {
                            "type": "output_text",
                            "text": json.dumps({
                                "signals": [
                                    {
                                        "symbol": "AXTI",
                                        "company": "AXT Inc",
                                        "thesis": "InP衬底供应商，AI光互连需求爆发",
                                        "conviction": "high",
                                        "sector_theme": "CPO/光互连",
                                        "chokepoint_signal": True,
                                    }
                                ],
                                "macro_view": "AI基础设施投资持续",
                                "post_count": 3,
                            }),
                            "annotations": [
                                {
                                    "type": "url_citation",
                                    "title": "Serenity on InP",
                                    "url": "https://x.com/aleabitoreddit/status/123",
                                }
                            ],
                        }
                    ],
                }
            ]
        }

        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.post.return_value = mock_response
            mock_client_cls.return_value = mock_client

            from trade_compass_agent.runtime.tools.search import tool_search_x_kol
            result = json.loads(tool_search_x_kol())

        assert result["provider"] == "xai_kol_signals"
        assert result["handles"] == ["aleabitoreddit"]
        assert len(result["signals"]) == 1
        assert result["signals"][0]["symbol"] == "AXTI"
        assert result["signals"][0]["chokepoint_signal"] is True
        assert result["macro_view"] == "AI基础设施投资持续"
        assert len(result["citations"]) == 1

    def test_custom_handles_and_topic(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("XAI_API_KEY", "test-key")

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "output": [
                {
                    "type": "message",
                    "content": [
                        {
                            "type": "output_text",
                            "text": '{"signals": [], "macro_view": "", "post_count": 0}',
                            "annotations": [],
                        }
                    ],
                }
            ]
        }

        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.post.return_value = mock_response
            mock_client_cls.return_value = mock_client

            from trade_compass_agent.runtime.tools.search import tool_search_x_kol
            result = json.loads(tool_search_x_kol(
                handles=["someuser", "anotheruser"],
                topic="人形机器人",
                days_back=7,
            ))

        assert result["handles"] == ["someuser", "anotheruser"]
        assert result["topic"] == "人形机器人"

    def test_non_json_response_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("XAI_API_KEY", "test-key")

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "output": [
                {
                    "type": "message",
                    "content": [
                        {
                            "type": "output_text",
                            "text": "Serenity最近看好CPO赛道中的光芯片公司。",
                            "annotations": [],
                        }
                    ],
                }
            ]
        }

        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.post.return_value = mock_response
            mock_client_cls.return_value = mock_client

            from trade_compass_agent.runtime.tools.search import tool_search_x_kol
            result = json.loads(tool_search_x_kol())

        assert result["signals"] == []
        assert "raw_analysis" in result
        assert "CPO" in result["raw_analysis"]
        assert "parse_note" in result


# ---------------------------------------------------------------------------
# chokepoint_analyst specialist tests
# ---------------------------------------------------------------------------

class TestChokepointAnalyst:
    """Tests for chokepoint_analyst specialist."""

    def test_returns_structured_report(self, specialist_stack: MarketStack) -> None:
        class _MockClient:
            name = "mock"
            messages = None
            tools = None

            def complete(self, messages, *, tools=None):
                self.messages = messages
                self.tools = tools
                return ChatCompletion(
                    content=(
                        "### 需求波\nAI算力扩张\n"
                        "### 架构变迁\n铜退光进\n"
                        "### 产业链地图\nL1: InP衬底\n"
                        "### 瓶颈节点\nAXTI\n"
                        "### Chokepoint Score: 78/100\n"
                        "### 关键证据\n强: 10-K披露产能\n"
                        "### 风险与证伪条件\n第二供应商崛起\n"
                        "### KOL 信号\nSerenity看好"
                    ),
                    model="mock",
                    provider="mock",
                )

        client = _MockClient()
        result = run_asset_specialist(
            specialist_stack,
            load_specialist_profiles()["chokepoint_analyst"],
            "分析AXTI在AI光互连产业链中的卡脖子属性",
            client=client,
        )

        assert "需求波" in result
        assert "Chokepoint Score" in result
        assert "瓶颈节点" in result
        assert client.messages is not None
        assert "Chokepoint Analyst Specialist" in client.messages[0].content
        assert "紫苏叶理论" in client.messages[0].content
        assert client.tools is not None
        assert {schema["function"]["name"] for schema in client.tools} >= {
            "search_x_kol",
            "web_search",
            "search_announcements",
        }

    def test_tool_execution(self, specialist_stack: MarketStack) -> None:
        call_log: list[str] = []
        call_count = 0

        class _ToolThenAnswerClient:
            name = "mock"

            def complete(self, messages, *, tools=None):
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    return ChatCompletion(
                        content=None,
                        tool_calls=[
                            ToolCall(
                                id="call_kol",
                                name="search_x_kol",
                                arguments='{"topic": "CPO"}',
                            )
                        ],
                        model="mock",
                        provider="mock",
                    )
                return ChatCompletion(
                    content=(
                        "### 需求波\nAI\n### 架构变迁\nCPO\n"
                        "### 产业链地图\nInP\n### 瓶颈节点\nAXTI\n"
                        "### Chokepoint Score: 72/100\n"
                        "### 关键证据\n中: 行业报告\n"
                        "### 风险与证伪条件\n技术路线风险\n"
                        "### KOL 信号\n无"
                    ),
                    model="mock",
                    provider="mock",
                )

        def _on_event(event):
            if event.event == "tool_start":
                call_log.append(event.data.get("tool"))

        with patch(
            "trade_compass_agent.runtime.tools.search.tool_search_x_kol",
            return_value='{"signals": [], "macro_view": "", "provider": "xai_kol_signals"}',
        ):
            result = run_asset_specialist(
                specialist_stack,
                load_specialist_profiles()["chokepoint_analyst"],
                "分析CPO产业链瓶颈",
                client=_ToolThenAnswerClient(),
                on_event=_on_event,
            )

        assert "search_x_kol" in call_log
        assert "Chokepoint Score" in result

    def test_registered_and_dispatchable(self, specialist_stack: MarketStack) -> None:
        """Verify chokepoint_analyst appears in specialist registry."""
        from trade_compass_agent.runtime.specialists.registry import list_specialists

        ids = [s.id for s in list_specialists()]
        assert "chokepoint_analyst" in ids

    def test_unavailable_llm(self, specialist_stack: MarketStack) -> None:
        with patch(
            "trade_compass_agent.runtime.specialists.asset_runner.create_chat_client",
            side_effect=Exception("No API key"),
        ):
            from trade_compass_agent.runtime.exceptions import AgentUnavailableError

            with patch(
                "trade_compass_agent.runtime.specialists.asset_runner.create_chat_client",
                side_effect=AgentUnavailableError("No API key"),
            ):
                result = run_asset_specialist(
                    specialist_stack,
                    load_specialist_profiles()["chokepoint_analyst"],
                    "test",
                )
                assert "unavailable" in result
