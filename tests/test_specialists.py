from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from trade_compass_agent.config import load_app_config
from trade_compass_agent.llm.providers import ChatCompletion, ToolCall
from trade_compass_agent.runtime.market_stack import MarketStack
from trade_compass_agent.runtime.specialists.assets import ExecutionModel, load_specialist_profiles
from trade_compass_agent.runtime.specialists.asset_runner import run_asset_specialist
from trade_compass_agent.runtime.specialists.multi_agent.assets import (
    load_multi_agent_plan_asset,
)
from trade_compass_agent.runtime.specialists.multi_agent.engine import default_multi_agent_engine
from trade_compass_agent.runtime.specialists.multi_agent.types import RunState, TeamSpec
from trade_compass_agent.runtime.specialists.registry import get_specialist
from trade_compass_agent.runtime.specialists.run import run_specialist
from trade_compass_agent.runtime.specialists.signal_parsing import parse_screener_signals
from trade_compass_agent.runtime.types import TurnEvent


class _MockChatClient:
    name = "mock"

    def __init__(self, content: str) -> None:
        self._content = content
        self.messages = None
        self.tools = None

    def complete(self, messages, *, tools=None) -> ChatCompletion:
        self.messages = messages
        self.tools = tools
        return ChatCompletion(content=self._content, model="mock", provider="mock")


@pytest.fixture()
def specialist_stack(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> MarketStack:
    monkeypatch.setenv("TRADE_COMPASS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("TRADE_COMPASS_MEMORY_DIR", str(tmp_path / "memory"))
    monkeypatch.setenv("TRADE_COMPASS_DATA_PROVIDER", "sample")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    config = load_app_config()
    return MarketStack.from_config(config)


def test_intraday_tech_returns_markdown(specialist_stack: MarketStack) -> None:
    mock_client = _MockChatClient(
        "## 结构\n震荡偏多\n\n## 动量\n量比 1.2\n\n## 风险\n追高\n\n## 操作建议\n观望"
    )
    result = run_asset_specialist(
        specialist_stack,
        load_specialist_profiles()["intraday_tech"],
        "分析 600519 5分钟走势",
        client=mock_client,
    )
    assert result.strip()
    assert "##" in result or "结构" in result
    assert mock_client.messages is not None
    system_prompt = mock_client.messages[0].content
    assert "Intraday Technical Specialist" in system_prompt
    assert "## Skill: intraday-tech" in system_prompt
    assert "(skill intraday-tech not found)" not in system_prompt
    assert mock_client.tools is not None
    assert {schema["function"]["name"] for schema in mock_client.tools} >= {
        "get_bars",
        "chart_pattern",
        "compute_ma",
    }


def test_macro_sentiment_calls_fund_flow(specialist_stack: MarketStack) -> None:
    """Verify macro_sentiment specialist can invoke get_fund_flow and produce output."""
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
                            id="call_1",
                            name="get_fund_flow",
                            arguments='{"category": "summary", "limit": 5}',
                        )
                    ],
                    model="mock",
                    provider="mock",
                )
            return ChatCompletion(
                content="### 市场水温\n震荡\n### 宏观信号\n宽松\n### 资金面\n主力净流入\n### 风险提示\n无",
                model="mock",
                provider="mock",
            )

    def _on_event(event):
        if event.event == "tool_start":
            call_log.append(event.data.get("tool"))

    from unittest.mock import patch
    from trade_compass_agent.data.fund_flow import FundFlowProvider

    with patch.object(FundFlowProvider, "get_stock_main_flow", return_value=[]):
        with patch.object(FundFlowProvider, "get_sector_flow", return_value=[]):
            result = run_asset_specialist(
                specialist_stack,
                load_specialist_profiles()["macro_sentiment"],
                "分析当前资金面",
                client=_ToolThenAnswerClient(),
                on_event=_on_event,
            )

    assert "资金面" in result
    assert "get_fund_flow" in call_log


def test_risk_advisor_uses_asset_declared_tools(specialist_stack: MarketStack) -> None:
    call_log: list[str] = []
    call_count = 0

    class _RiskToolClient:
        name = "mock"

        def complete(self, messages, *, tools=None):
            nonlocal call_count
            call_count += 1
            tool_names = {schema["function"]["name"] for schema in tools or []}
            assert "get_risk_status" in tool_names
            assert "get_market_constraints" in tool_names
            if call_count == 1:
                return ChatCompletion(
                    content=None,
                    tool_calls=[
                        ToolCall(
                            id="risk_1",
                            name="get_risk_status",
                            arguments="{}",
                        )
                    ],
                    model="mock",
                    provider="mock",
                )
            return ChatCompletion(
                content="### 风险评估\n低\n### 否决清单\n无\n### 风险动作\n保持",
                model="mock",
                provider="mock",
            )

    def _on_event(event):
        if event.event == "tool_start":
            call_log.append(event.data.get("tool"))

    result = run_asset_specialist(
        specialist_stack,
        load_specialist_profiles()["risk_advisor"],
        "检查当前组合风险",
        client=_RiskToolClient(),
        on_event=_on_event,
    )

    assert "风险评估" in result
    assert "get_risk_status" in call_log


def test_screener_uses_asset_prompt_and_parses_signals(specialist_stack: MarketStack) -> None:
    class _ScreenerClient:
        name = "mock"
        messages = None
        tools = None

        def complete(self, messages, *, tools=None):
            self.messages = messages
            self.tools = tools
            return ChatCompletion(
                content=(
                    "### 600519\n"
                    "**Rating**: buy\n"
                    "**Confidence**: 0.72\n"
                    "**Entry Price**: 1500\n"
                    "**Stop Loss**: 1420\n"
                    "**Target Price**: 1680\n"
                    "**Reasoning**: MA20 支撑良好 [compute_ma]。"
                ),
                model="mock",
                provider="mock",
            )

    client = _ScreenerClient()
    task = (
        "## 候选列表（共 1 只）\n"
        "请依次分析以下候选股票，使用工具获取数据后给出结构化评级：\n"
        "600519"
    )
    report = run_asset_specialist(
        specialist_stack,
        load_specialist_profiles()["screener"],
        task,
        client=client,
    )
    signals = parse_screener_signals(report, ["600519"])

    assert len(signals) == 1
    assert signals[0].symbol == "600519"
    assert signals[0].rating.value == "buy"
    assert signals[0].source_specialist == "screener"
    assert client.messages is not None
    assert "Screener Specialist" in client.messages[0].content
    assert client.tools is not None
    assert {schema["function"]["name"] for schema in client.tools} >= {
        "get_bars",
        "get_fundamentals",
        "compute_ma",
    }


def test_screener_signal_parser_accepts_json_output() -> None:
    report = json.dumps(
        {
            "signals": [
                {
                    "symbol": "600519",
                    "rating": "buy",
                    "confidence": 0.72,
                    "entry_price": 1500,
                    "stop_loss": 1420,
                    "target_price": 1680,
                    "reasoning": "MA20 支撑良好 [compute_ma]。",
                }
            ],
            "warnings": [],
            "metadata": {"specialist_id": "screener", "execution_model": "single_agent_react"},
        },
        ensure_ascii=False,
    )

    signals = parse_screener_signals(report, ["600519"])

    assert len(signals) == 1
    assert signals[0].symbol == "600519"
    assert signals[0].rating.value == "buy"
    assert signals[0].confidence == 0.72
    assert signals[0].risk_reward_ratio == 2.25


def test_screener_signal_parser_accepts_numbered_headings_and_tables() -> None:
    heading_report = (
        "### 1. 600519 贵州茅台\n"
        "**Rating**: hold\n"
        "**Confidence**: 0.51\n"
        "**Entry Price**: N/A\n"
        "**Stop Loss**: N/A\n"
        "**Target Price**: N/A\n"
        "**Reasoning**: 数据不足，先观察。\n"
    )
    table_report = (
        "| 股票 | 评级 | 置信度 | 核心逻辑 |\n"
        "|------|------|--------|---------|\n"
        "| **600519 贵州茅台** | **sell** | 0.65 | 趋势恶化，风险收益比不足 |\n"
    )

    heading_signals = parse_screener_signals(heading_report, ["600519"])
    table_signals = parse_screener_signals(table_report, ["600519"])

    assert heading_signals[0].rating.value == "hold"
    assert heading_signals[0].entry_price is None
    assert table_signals[0].rating.value == "sell"
    assert table_signals[0].confidence == 0.65


def test_intraday_tech_calls_chart_pattern(specialist_stack: MarketStack) -> None:
    """Verify intraday_tech specialist can invoke chart_pattern tool."""
    call_log: list[str] = []
    call_count = 0

    class _ChartPatternClient:
        name = "mock"

        def complete(self, messages, *, tools=None):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return ChatCompletion(
                    content=None,
                    tool_calls=[
                        ToolCall(
                            id="call_cp",
                            name="chart_pattern",
                            arguments='{"symbol": "600519", "bars": 40}',
                        )
                    ],
                    model="mock",
                    provider="mock",
                )
            return ChatCompletion(
                content="## 结构\n上升通道\n## 动量\nRSI 55\n## 形态\n双底形态 [chart_pattern]\n## 风险\n无\n## 操作建议\n持有",
                model="mock",
                provider="mock",
            )

    def _on_event(event):
        if event.event == "tool_start":
            call_log.append(event.data.get("tool"))

    from unittest.mock import patch

    with patch(
        "trade_compass_agent.runtime.tools.chart_pattern.tool_chart_pattern",
        return_value='{"patterns": ["双底"], "trend": "上升", "confidence": 0.75}',
    ) as mock_fn:
        result = run_asset_specialist(
            specialist_stack,
            load_specialist_profiles()["intraday_tech"],
            "分析 600519 日线形态",
            client=_ChartPatternClient(),
            on_event=_on_event,
        )

    assert "chart_pattern" in call_log
    mock_fn.assert_called_once_with(specialist_stack, symbol="600519", bars=40)
    assert "形态" in result


def test_unknown_specialist_returns_error_json(specialist_stack: MarketStack) -> None:
    result = run_specialist(specialist_stack, "unknown_bot", "do something")
    payload = json.loads(result)
    assert "error" in payload
    assert "unknown specialist" in payload["error"]
    assert "available" in payload


def test_equity_research_specialist_profile_loads_from_asset_folder() -> None:
    profiles = load_specialist_profiles()
    expected_ids = {
        "equity_research",
        "intraday_tech",
        "risk_advisor",
        "screener",
        "debate",
        "macro_sentiment",
        "chokepoint_analyst",
    }
    assert expected_ids <= set(profiles)

    profile = profiles["equity_research"]
    assert profile.id == "equity_research"
    assert profile.kind == "specialist"
    assert profile.execution_model.type == "debate_team"
    assert profile.execution_model.plan == "debate_v2"
    assert "get_bars" in profile.capabilities.tools
    assert profile.output.schema == "schemas/report.schema.json"
    assert profile.prompts["system"].strip()
    assert profiles["intraday_tech"].execution_model.type == "single_agent_react"
    assert profiles["risk_advisor"].execution_model.type == "single_agent_react"
    assert profiles["screener"].execution_model.type == "single_agent_react"
    assert profiles["debate"].execution_model.type == "debate_team"
    assert profiles["debate"].execution_model.plan == "debate_v2"
    assert profiles["macro_sentiment"].execution_model.type == "single_agent_react"
    assert profiles["chokepoint_analyst"].execution_model.type == "single_agent_react"


def test_specialist_catalog_carries_asset_profiles() -> None:
    expected = {
        "equity_research": "debate_team",
        "intraday_tech": "single_agent_react",
        "risk_advisor": "single_agent_react",
        "screener": "single_agent_react",
        "debate": "debate_team",
        "macro_sentiment": "single_agent_react",
        "chokepoint_analyst": "single_agent_react",
    }
    for specialist_id, execution_model in expected.items():
        profile = get_specialist(specialist_id)
        assert profile is not None
        assert profile.execution_model.type == execution_model


def test_dispatch_specialists_tool_schema_lists_folder_backed_assets(
    specialist_stack: MarketStack,
) -> None:
    from trade_compass_agent.runtime.tools.registry import ToolRegistry

    schema = next(
        item
        for item in ToolRegistry(specialist_stack).schemas
        if item["function"]["name"] == "dispatch_specialists"
    )
    description = schema["function"]["description"]

    assert "Available folder-backed specialists" in description
    assert "equity_research" in description
    assert "chokepoint_analyst" in description


def test_dispatch_specialists_events_include_execution_model(
    specialist_stack: MarketStack,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from trade_compass_agent.runtime.tools import dispatch

    events: list[TurnEvent] = []
    monkeypatch.setattr(dispatch, "run_specialist", lambda *args, **kwargs: "risk report")

    payload = dispatch.tool_dispatch_specialists(
        specialist_stack,
        [{"specialist": "risk_advisor", "task": "检查组合风险"}],
        on_event=events.append,
    )

    assert json.loads(payload)["results"][0]["output"] == "risk report"
    started = next(event for event in events if event.event == "specialist_started")
    done = next(event for event in events if event.event == "specialist_done")
    assert started.data["execution_model"] == "single_agent_react"
    assert done.data["execution_model"] == "single_agent_react"


def test_generic_asset_specialist_runner_uses_folder_profile(
    specialist_stack: MarketStack,
) -> None:
    profile = replace(
        load_specialist_profiles()["intraday_tech"],
        id="asset_only_demo",
        description="Asset-only demo specialist",
    )

    result = run_asset_specialist(
        specialist_stack,
        profile,
        "分析 600519",
        client=_MockChatClient("## 结论\n资产 runner 已执行"),
    )

    assert "资产 runner 已执行" in result


def test_asset_specialist_reader_tool_returns_reader_payload(
    specialist_stack: MarketStack,
) -> None:
    from trade_compass_agent.runtime.specialists.asset_runner import _execute_allowed_tool
    from trade_compass_agent.runtime.tools.registry import ToolRegistry

    profile = replace(
        load_specialist_profiles()["intraday_tech"],
        id="reader_asset_demo",
        description="Reader asset demo specialist",
        capabilities=replace(
            load_specialist_profiles()["intraday_tech"].capabilities,
            tools=("read_news",),
        ),
    )

    result = _execute_allowed_tool(
        ToolRegistry(specialist_stack),
        profile,
        "read_news",
        {
            "content": "600519 公司公告称业绩增长。",
            "source": "unit-test:specialist-reader",
        },
    )
    payload = json.loads(result)

    assert payload["reader_type"] == "news_reader"
    assert payload["source_refs"] == ["unit-test:specialist-reader"]


def test_asset_specialist_unsupported_multi_agent_strategy_returns_clear_error(
    specialist_stack: MarketStack,
) -> None:
    profile = replace(
        load_specialist_profiles()["equity_research"],
        id="graph_team_demo",
        description="Unsupported graph team demo specialist",
        execution_model=ExecutionModel(type="graph_team", plan="graph_v1"),
    )

    result = json.loads(run_asset_specialist(specialist_stack, profile, "分析 600519"))

    assert result["specialist"] == "graph_team_demo"
    assert result["execution_model"] == "graph_team"
    assert result["plan"] == "graph_v1"
    assert result["error"] == "unsupported multi-agent strategy: graph_team/graph_v1"


def test_multi_agent_engine_runs_debate_team_strategy(
    specialist_stack: MarketStack,
) -> None:
    events: list[TurnEvent] = []

    result = default_multi_agent_engine().run(
        RunState(
            team=TeamSpec(
                id="equity_research",
                strategy="debate_team",
                plan="debate_v2",
            ),
            task="分析 600519",
            stack=specialist_stack,
            config=specialist_stack.config,
            client=_MockChatClient("**Final Rating**: hold\n**Confidence**: 0.5\n**Reasoning**: mock"),
            on_event=events.append,
        )
    )

    assert "PM 决策" in result.output
    assert result.metadata["symbols"] == ["600519"]
    assert result.metadata["plan_asset"] == "debate_v2"
    event_names = [event.event for event in events]
    assert "multi_agent.started" in event_names
    assert "multi_agent.symbol_started" in event_names
    assert "multi_agent.symbol_finished" in event_names
    assert "multi_agent.finished" in event_names


def test_debate_team_strategy_requires_specialist_plan_asset(
    specialist_stack: MarketStack,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from trade_compass_agent.runtime.specialists.multi_agent.strategies import debate_team
    from trade_compass_agent.runtime.specialists.multi_agent.assets import PlanAssetError

    def missing_asset(*args, **kwargs):
        raise PlanAssetError("missing plan asset")

    monkeypatch.setattr(debate_team, "load_multi_agent_plan_asset", missing_asset)

    result = default_multi_agent_engine().run(
        RunState(
            team=TeamSpec(
                id="equity_research",
                strategy="debate_team",
                plan="debate_v2",
            ),
            task="分析 600519",
            stack=specialist_stack,
            config=specialist_stack.config,
        )
    )

    payload = json.loads(result.output)
    assert payload["specialist"] == "equity_research"
    assert "plan asset unavailable" in payload["error"]
    assert result.metadata["plan_asset"] == ""
    assert result.warnings


def test_debate_v2_plan_asset_loads_from_specialist_folder() -> None:
    asset = load_multi_agent_plan_asset("equity_research", "debate_v2")

    assert asset.id == "debate_v2"
    assert asset.strategy == "debate_team"
    assert asset.defaults["max_debate_rounds"] == 3
    assert asset.stages["research"].agents == (
        "technical_analyst",
        "fundamental_analyst",
        "sentiment_analyst",
        "supply_chain_analyst",
    )
    assert "紫苏叶理论" in asset.prompt("supply_chain_analyst")
    assert "get_bars" in asset.agents["technical_analyst"].tools


def test_plan_asset_loader_requires_agent_prompt(tmp_path: Path) -> None:
    from trade_compass_agent.runtime.specialists.multi_agent.assets import (
        PlanAssetError,
        load_plan_asset,
    )

    plan_root = tmp_path / "plans"
    plan_root.mkdir()
    (plan_root / "bad.yaml").write_text(
        "\n".join(
            [
                "id: bad",
                "version: 1",
                "strategy: debate_team",
                "agents:",
                "  analyst:",
                "    role: analyst",
                "    tools: []",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(PlanAssetError, match="prompt is required"):
        load_plan_asset(plan_root / "bad.yaml")
