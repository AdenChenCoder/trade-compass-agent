from __future__ import annotations

import json

import pytest

from trade_compass_agent.config import AppConfig, LLMConfig
from trade_compass_agent.llm.native import NativeAgentClient
from trade_compass_agent.llm.providers import ChatCompletion, ToolCall
from trade_compass_agent.runtime import loop as loop_module
from trade_compass_agent.runtime.loop import AgentLoop
from trade_compass_agent.runtime.market_stack import MarketStack
from trade_compass_agent.runtime.specialists.asset_runner import run_asset_specialist
from trade_compass_agent.runtime.specialists.assets import load_specialist_profiles
from trade_compass_agent.runtime.specialists.multi_agent.strategies import debate_team
from trade_compass_agent.runtime.tools.dispatch import tool_dispatch_specialists


@pytest.mark.parametrize("consumer", ["asset", "dispatch", "agent"])
@pytest.mark.parametrize("incomplete", [False, True])
def test_debate_incomplete_research_reaches_consumer(tmp_path, monkeypatch, consumer, incomplete):
    config = AppConfig(data_dir=tmp_path / "data", memory_dir=tmp_path / "memory", data_provider="sample",
                       llm=LLMConfig(provider="codex", model=""))
    stack = MarketStack.from_config(config)
    reason = "技术研究未完成：已达到工具预算。"
    available = "已保留技术研究中取得的日线信息。"
    stages = []

    def stage(**kwargs):
        agent = kwargs["agent"].id
        stages.append(agent)
        if agent == "technical_analyst":
            if incomplete:
                return json.dumps({"error": reason, "incomplete_reason": reason,
                                   "report_markdown": available, "warnings": [reason]}, ensure_ascii=False)
            return available
        return f"{agent} 的可用研究内容。"

    monkeypatch.setattr(debate_team, "_run_agent", stage)
    monkeypatch.setattr(debate_team, "_chat_client", lambda *a, **kw: object())
    monkeypatch.setattr(debate_team, "build_situation_summary", lambda stack: "隔离研究情境")
    events = []
    if consumer == "asset":
        # Two symbols prove that report concatenation does not discard status.
        output = run_asset_specialist(stack, load_specialist_profiles()["debate"],
                                      "分析 600519, 000858", config=config, on_event=events.append)
    elif consumer == "dispatch":
        dispatched = json.loads(tool_dispatch_specialists(stack,
            [{"specialist": "debate", "task": "分析600519"}], config=config, on_event=events.append))
        output = dispatched["results"][0]["output"]
        assert [e.data["status"] for e in events if e.event == "specialist_done"] == ["error" if incomplete else "ok"]
    else:
        learned, reviews = [], []

        class MainClient(NativeAgentClient):
            def __init__(self):
                self.name, self.model = "codex", ""

            def run_agent(self, messages, *, tools, execute_tool, **kwargs):
                if tools:
                    execute_tool(ToolCall(id="debate-call", name="dispatch_specialists",
                        arguments=json.dumps({"tasks": [{"specialist": "debate", "task": "分析600519"}]})))
                # The application must retain the child status even if the main
                # model does not repeat the incomplete warning itself.
                return ChatCompletion(content="以下为已取得的研究内容。")

        monkeypatch.setattr(loop_module, "create_chat_client", lambda cfg: MainClient())
        monkeypatch.setattr(loop_module, "curate_turn_insight", lambda **kw: learned.append(kw))
        monkeypatch.setattr(AgentLoop, "_maybe_background_review", lambda *a, **kw: reviews.append(kw))
        response = AgentLoop.from_config(config, on_event=events.append).run_turn("请安排专家研究600519")
        assert response.degraded is incomplete
        assert bool(learned) is not incomplete
        assert bool(reviews) is not incomplete
        if incomplete:
            assert reason in response.summary and reason in response.warnings
        assert events[-1].event == "done" and events[-1].data["degraded"] is incomplete
        return

    assert "pm_judge" in stages  # Later synthesis cannot erase earlier gaps.
    if incomplete:
        payload = json.loads(output)
        assert payload["incomplete_reason"] == payload["error"] == reason
        assert payload["warnings"] == [reason]
        assert available in payload["report_markdown"]
        assert reason in payload["report_markdown"]
        assert '"report_markdown"' not in payload["report_markdown"]
        if consumer == "asset":
            assert "# 600519" in payload["report_markdown"] and "# 000858" in payload["report_markdown"]
    else:
        assert available in output and reason not in output
