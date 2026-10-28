"""Consumer checks for dated planning references without live quote privileges."""
from dataclasses import replace
from datetime import date, datetime
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from trade_compass_agent.config import AgentConfig, AppConfig, LLMConfig
from trade_compass_agent.domain import AccountKind, Bar, PaperTrade
from trade_compass_agent.portfolio import JsonPaperPortfolio
from trade_compass_agent.portfolio.planning import planning_close_references, premarket_reference_date
from trade_compass_agent.runtime.report_quality import assess_data_quality, report_facts
from trade_compass_agent.runtime.tools.registry import ToolRegistry

DAY = date(2026, 10, 26)
PREVIOUS = date(2026, 10, 23)
BAR = Bar("003021", datetime(2026, 10, 23, 15), 85, 91.73, 85, 91.73, 100, source="tencent")


@pytest.mark.parametrize("now, expected", [
    (datetime(2026, 10, 26, 9, 17), PREVIOUS),
    (datetime(2026, 10, 26, 9, 30), None),
    (datetime(2026, 10, 26, 10), None),
    (datetime(2026, 10, 27, 9), None),
])
def test_only_current_premarket_uses_previous_session(now, expected):
    assert premarket_reference_date(DAY, now=now) == expected


def test_previous_session_observes_holiday_and_weekend():
    assert premarket_reference_date(date(2026, 10, 9), now=datetime(2026, 10, 9, 9)) == date(2026, 9, 30)
    assert premarket_reference_date(date(2026, 10, 25), now=datetime(2026, 10, 25, 9)) is None


@pytest.mark.parametrize("bar", [
    replace(BAR, timestamp=datetime(2026, 10, 22, 15)),
    replace(BAR, timestamp=datetime(2026, 10, 26, 15)),
    replace(BAR, symbol="600519"), replace(BAR, source="sample"),
    replace(BAR, source=None), replace(BAR, close=float("nan")), replace(BAR, close=0),
])
def test_invalid_reference_remains_a_gap(bar):
    assert planning_close_references(SimpleNamespace(get_bars=lambda *a, **k: [bar]), ["003021"], PREVIOUS) == {}


def test_empty_partial_and_failed_queries_stay_independent():
    provider = Mock()
    assert planning_close_references(provider, [], PREVIOUS) == {}
    provider.get_bars.assert_not_called()

    def read(symbol, **kwargs):
        if symbol == "600519":
            raise TimeoutError("unavailable")
        return [BAR]

    provider.get_bars.side_effect = read
    refs = planning_close_references(provider, ["003021", "600519"], PREVIOUS)
    assert set(refs) == {"003021"}
    assert refs["003021"] == {"date": "2026-10-23", "close": 91.73, "source": "tencent",
                              "adjusted": False, "purpose": "planning_only"}


def test_slow_reference_fetch_is_bounded():
    import time
    def read(*args, **kwargs):
        time.sleep(0.2)
        return [BAR]
    started = time.monotonic()
    assert planning_close_references(SimpleNamespace(get_bars=read), ["003021"], PREVIOUS, timeout=0.01) == {}
    assert time.monotonic() - started < 0.15


def setup_registry(tmp_path, monkeypatch, bars=None):
    config = AppConfig(data_dir=tmp_path / "data", memory_dir=tmp_path / "memory", data_provider="sample",
                       llm=LLMConfig(provider="disabled"), agent=AgentConfig(learning_enabled=False, llm_session_titles=False))
    config.data_dir.mkdir(parents=True, exist_ok=True)
    portfolio = JsonPaperPortfolio(config.data_dir / "paper_trades.jsonl")
    portfolio.record(PaperTrade(symbol="003021", account=AccountKind.SHORT_STOCK, side="buy", quantity=100,
                               price=88.44, timestamp=datetime(2026, 10, 23, 9, 37), reason="test"))
    provider = Mock(get_bars=Mock(return_value=[BAR] if bars is None else bars))
    registry = ToolRegistry(SimpleNamespace(config=config, provider=provider))
    registry.planning_reference_date = PREVIOUS
    monkeypatch.setattr(JsonPaperPortfolio, "positions_with_market_prices", lambda *a, **k: pytest.fail("planning requested live valuation"))
    return registry, config


def test_real_tool_keeps_reference_separate_and_does_not_change_trades(tmp_path, monkeypatch):
    from trade_compass_agent.runtime.tools.builtin_operations import _compact_positions
    registry, config = setup_registry(tmp_path, monkeypatch)
    path = config.data_dir / "paper_trades.jsonl"
    before = path.read_bytes()
    result = registry.execute("analyze_portfolio", {})
    row = json.loads(result)["positions"][0]
    assert row["last_price"] == 88.44 and row["price_is_fresh"] is False
    assert row["planning_reference"]["close"] == 91.73
    assert _compact_positions([row])[0]["planning_reference"] == row["planning_reference"]
    assert "price" not in report_facts([("analyze_portfolio", result)]).get("003021", {})
    assert path.read_bytes() == before
    assert not assess_data_quality([("analyze_portfolio", result)], planning_reference_date="2026-10-23").degraded
    assert assess_data_quality([("analyze_portfolio", result)]).degraded
    assert assess_data_quality([("analyze_portfolio", result)], planning_reference_date="2026-10-22").degraded


@pytest.mark.parametrize("has_reference", [True, False])
def test_scheduled_plan_publishes_reference_or_visible_gap(tmp_path, monkeypatch, has_reference):
    from trade_compass_agent.llm.providers import ChatCompletion, ToolCall
    from trade_compass_agent.ops.agent_session import ScheduledAgentSession
    from trade_compass_agent.runtime import loop

    registry, config = setup_registry(tmp_path, monkeypatch, bars=[BAR] if has_reference else [])
    monkeypatch.setattr("trade_compass_agent.portfolio.planning.premarket_reference_date", lambda day: PREVIOUS)
    monkeypatch.setattr(loop, "shared_consolidation_worker", lambda **_: None)
    replies = [ChatCompletion(content=None, tool_calls=[ToolCall("read", "analyze_portfolio", "{}")]),
               ChatCompletion(content="003021盘前参考2026-10-23收盘价91.73，非实时报价。" if has_reference else "持仓报价存在数据缺口。")]
    monkeypatch.setattr(loop, "create_chat_client", lambda *_: SimpleNamespace(stream_complete=lambda *a, **k: replies.pop(0)))
    real_from_config = loop.AgentLoop.from_config

    def make_agent(*args, **kwargs):
        agent = real_from_config(*args, **kwargs)
        agent._tools.stack = registry.stack
        return agent

    monkeypatch.setattr(loop.AgentLoop, "from_config", make_agent)
    session = ScheduledAgentSession(config, job_id="morning_plan", step_id="agent_plan", run_date=DAY,
                                    tool_whitelist={"analyze_portfolio"})
    text = session.run("生成盘前计划", timeout=20)
    assert session.degraded is (not has_reference)
    assert ("2026-10-23" in text) is has_reference
    assert "交易前须重新核验" in text if has_reference else "持仓价格未更新" in text
    saved = (config.data_dir / "agent_sessions" / f"{session.session_id}.jsonl").read_text()
    assert text in [json.loads(line).get("content") for line in saved.splitlines()]
    assert not replies


@pytest.mark.parametrize("has_reference", [True, False])
def test_workflow_plan_checks_upstream_even_without_another_tool_read(tmp_path, monkeypatch, has_reference):
    import asyncio
    from trade_compass_agent.llm.providers import ChatCompletion
    from trade_compass_agent.ops.agent_session import run_agent_step
    from trade_compass_agent.ops.job_definition import StepContext, StepOutput
    from trade_compass_agent.runtime import loop

    registry, config = setup_registry(tmp_path, monkeypatch, bars=[BAR] if has_reference else [])
    snapshot = json.loads(registry.execute("analyze_portfolio", {}))
    monkeypatch.setattr("trade_compass_agent.portfolio.planning.premarket_reference_date", lambda day: PREVIOUS)
    monkeypatch.setattr(loop, "shared_consolidation_worker", lambda **_: None)
    monkeypatch.setattr(loop, "create_chat_client", lambda *_: SimpleNamespace(
        stream_complete=lambda *a, **k: ChatCompletion(content="盘前观察计划。")))
    ctx = StepContext(config, DAY, upstream={"portfolio_check": StepOutput(message="持仓", data=snapshot)})
    result = asyncio.run(run_agent_step(ctx, "盘前计划", "morning_plan", step_id="agent_plan", tool_whitelist=set()))
    assert result.data["degraded"] is (not has_reference)
    assert "2026-10-23" in result.data["analysis"] if has_reference else "持仓价格未更新" in result.data["analysis"]
