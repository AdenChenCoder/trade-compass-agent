"""Prefix stability, reasoning replay, and serial local specialist dispatch."""

import json
import threading
import time
from types import SimpleNamespace

import httpx

from trade_compass_agent.config import AgentConfig, AppConfig, LLMConfig
from trade_compass_agent.llm.providers import (
    ChatCompletion, ChatMessage, OpenAIChatClient, _tool_choice, llm_tool_choice,
)
from trade_compass_agent.runtime import loop
from trade_compass_agent.runtime.tools import dispatch


def _agent(tmp_path, stream, *, max_tool_rounds=30):
    config = AppConfig(
        data_dir=tmp_path / "data",
        memory_dir=tmp_path / "memory",
        data_provider="sample",
        llm=LLMConfig(provider="lmstudio", model="qwen3.8-27b"),
        agent=AgentConfig(max_tool_rounds=max_tool_rounds, learning_enabled=False, llm_session_titles=False),
    )
    return config, loop.AgentLoop.from_config(config)


def test_session_prefix_stays_stable_when_trading_flag_changes(tmp_path, monkeypatch):
    requests = []
    versions = {"n": 0}

    def description():
        versions["n"] += 1
        return f"specialists-version-{versions['n']}"

    def stream(messages, **kwargs):
        requests.append((list(messages), kwargs.get("tools")))
        return ChatCompletion(content="本轮只汇报已有资料，没有成交。")

    monkeypatch.setattr(loop, "create_chat_client", lambda *_: SimpleNamespace(
        stream_complete=stream, name="lmstudio", model="qwen3.8-27b",
    ))
    monkeypatch.setattr(loop, "shared_consolidation_worker", lambda **_: None)
    monkeypatch.setattr(
        "trade_compass_agent.runtime.tools.registry._dispatch_specialists_description",
        description,
    )
    prompts = iter(["SYSTEM_V1", "SYSTEM_V2"])
    monkeypatch.setattr(loop, "build_stable_system_prompt", lambda: next(prompts))
    config, agent = _agent(tmp_path, stream)
    from trade_compass_agent.portfolio.trading_policy import AutonomousTradingStore

    assert "没有成交" in agent.run_turn("先看持仓", session_id="prefix-session").summary
    AutonomousTradingStore(config.data_dir).set_enabled(True)
    assert "没有成交" in agent.run_turn("再看一遍", session_id="prefix-session").summary

    first, first_tools = requests[0]
    second, second_tools = requests[1]
    assert first[0].content == second[0].content == "SYSTEM_V1"
    assert "全局 Agent 自主交易：" not in first[0].content
    assert [(m.role, m.content, m.name) for m in second[:len(first)]] == [
        (m.role, m.content, m.name) for m in first
    ]
    assert second[len(first)].role == "assistant"
    assert second[len(first) + 1].name == "session_state"
    assert "全局 Agent 自主交易：关闭" in first[1].content
    assert "全局 Agent 自主交易：开启" in second[len(first) + 1].content
    assert "specialists-version-1" in json.dumps(first_tools, ensure_ascii=False)
    assert json.dumps(first_tools, ensure_ascii=False) == json.dumps(second_tools, ensure_ascii=False)
    assert versions["n"] >= 2


def test_final_round_keeps_tool_definitions(tmp_path, monkeypatch):
    seen = []

    def stream(messages, **kwargs):
        seen.append((_tool_choice.get(), kwargs.get("tools")))
        return ChatCompletion(content="本轮没有调用工具，结论是继续观察。")

    monkeypatch.setattr(loop, "create_chat_client", lambda *_: SimpleNamespace(
        stream_complete=stream, name="lmstudio", model="qwen3.8-27b",
    ))
    monkeypatch.setattr(loop, "shared_consolidation_worker", lambda **_: None)
    _, agent = _agent(tmp_path, stream, max_tool_rounds=1)
    assert "继续观察" in agent.run_turn("今天怎么看", session_id="final-round").summary
    choice, tools = seen[0]
    assert choice == "none"
    assert any(item.get("function", {}).get("name") == "get_bars" for item in tools)


def test_wire_request_keeps_tools_when_choice_is_none(tmp_path, monkeypatch):
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        chunk = {
            "id": "fixture", "model": "qwen3.8-27b",
            "choices": [{"index": 0, "delta": {"content": "结论"}, "finish_reason": "stop"}],
        }
        body = "data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n"
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    monkeypatch.setattr(
        "trade_compass_agent.llm.providers._usage_http_client",
        lambda: httpx.Client(transport=httpx.MockTransport(handler)),
    )
    client = OpenAIChatClient(
        name="lmstudio", model="qwen3.8-27b", api_key="unit",
        base_url="http://localhost:1234/v1", usage_db_path=tmp_path / "usage.db",
    )
    tools = [{"type": "function", "function": {"name": "get_bars", "parameters": {"type": "object"}}}]
    with llm_tool_choice("none"):
        assert client.stream_complete(
            [ChatMessage(role="user", content="总结")], tools=tools,
        ).content == "结论"
    assert bodies[0]["tool_choice"] == "none"
    assert bodies[0]["tools"] == tools


def test_local_specialists_run_serially_and_remote_ones_stay_parallel(tmp_path, monkeypatch):
    def run_case(provider):
        active = 0
        peak = 0
        order = []
        lock = threading.Lock()

        def run(stack, name, task, **kwargs):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
                order.append(name)
            time.sleep(0.1)
            with lock:
                active -= 1
            return json.dumps({"specialist": name, "ok": True})

        monkeypatch.setattr(dispatch, "run_specialist", run)
        monkeypatch.setattr(dispatch, "build_situation_summary", lambda stack: {"summary": "done"})
        config = AppConfig(
            data_dir=tmp_path / provider, memory_dir=tmp_path / "memory",
            llm=LLMConfig(provider=provider, model="demo"),
        )
        stack = SimpleNamespace(config=config)
        payload = json.loads(dispatch.tool_dispatch_specialists(stack, [
            {"specialist": "one", "task": "分析"},
            {"specialist": "two", "task": "分析"},
        ]))
        names = [item["specialist"] for item in payload["results"]]
        return peak, order, names

    peak, order, names = run_case("lmstudio")
    assert (peak, order, names) == (1, ["one", "two"], ["one", "two"])
    assert run_case("ollama")[0] == 1
    peak, _order, names = run_case("openai")
    assert peak == 2 and names == ["one", "two"]


def test_local_debate_research_does_not_overlap(monkeypatch):
    from trade_compass_agent.runtime.specialists.multi_agent.assets import (
        MultiAgentPlanAsset, PlanAgentAsset, PlanStageAsset,
    )
    from trade_compass_agent.runtime.specialists.multi_agent.strategies import debate_team

    active = 0
    peak = 0
    lock = threading.Lock()

    def worker(**kwargs):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.05)
        with lock:
            active -= 1
        return kwargs["agent"].id

    agents = {key: PlanAgentAsset(id=key, role=key, prompt="research") for key in ("one", "two")}
    plan = MultiAgentPlanAsset(
        id="test", version=1, strategy="debate_team", description="",
        stages={"research": PlanStageAsset(id="research", agents=tuple(agents))}, agents=agents,
    )
    config = AppConfig(llm=LLMConfig(provider="lmstudio", model="qwen3.8-27b"))
    state = SimpleNamespace(team=SimpleNamespace(id="test"), on_event=None, config=config)
    monkeypatch.setattr(debate_team, "_run_agent", worker)
    result = debate_team._run_research_stage(
        state, plan_asset=plan, symbol="600519", context="",
        client=object(), registry=object(), variables={},
    )
    assert peak == 1 and set(result) == {"one", "two"}
