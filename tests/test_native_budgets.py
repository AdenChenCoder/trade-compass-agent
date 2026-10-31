from __future__ import annotations

import json
import sys
import time
from dataclasses import replace

import pytest

from test_native_agents import FAKE_CODEX
from trade_compass_agent.config import AppConfig, LLMConfig
from trade_compass_agent.llm.native import NativeAgentClient
from trade_compass_agent.llm.native_session import NativeConversation
from trade_compass_agent.llm.providers import ChatMessage, check_execution_active, llm_request_deadline
from trade_compass_agent.runtime.exceptions import AgentUnavailableError, TurnInterruptedError
from trade_compass_agent.runtime.loop import AgentLoop


@pytest.fixture
def budget_config(tmp_path, monkeypatch):
    binary = tmp_path / "codex"
    binary.write_text(f"#!{sys.executable}\n" + FAKE_CODEX)
    binary.chmod(0o700)
    config = AppConfig(data_dir=tmp_path / "data", memory_dir=tmp_path / "memory",
                       data_provider="sample", llm=LLMConfig(provider="codex", model="",
                       cli_path=str(binary), api_key_env="", timeout=5, agent_timeout=5,
                       native_max_tool_calls=2))
    monkeypatch.setattr(AgentLoop, "_maybe_background_review", lambda *a, **kw: None)
    monkeypatch.setenv("FAKE_PROTOCOL", str(tmp_path / "protocol.jsonl"))
    return replace(config, agent=replace(config.agent, max_tool_rounds=1))


def protocol(config):
    return [json.loads(line) for line in (config.data_dir.parent / "protocol.jsonl").read_text().splitlines()]


def started_turns(config):
    path = config.data_dir.parent / "protocol.jsonl"
    if not path.exists():
        return 0
    return sum(call.get("method") == "turn/start" for call in protocol(config))


def test_tool_budget_summarizes_receipts_and_preserves_history(budget_config, monkeypatch):
    monkeypatch.setenv("FAKE_TOOL_COUNT", "3")
    events = []
    loop = AgentLoop.from_config(budget_config, on_event=events.append)
    response = loop.run_turn("比较已有日线，未完成的部分请说明")
    assert response.summary.startswith("原生回答") and not response.interrupted
    assert response.degraded and "任务未全部完成" in response.summary
    assert any("工具预算" in warning for warning in response.warnings)
    session = loop.session_store.load(response.session_id)
    receipts = [m for m in session.messages if m.role == "tool"]
    assert len(receipts) == 2  # independent of the API backend's max_tool_rounds=1
    assert sum(e.event == "tool_start" for e in events) == 2
    assert events[-1].event == "done" and events[-1].data["ok"]
    calls = protocol(budget_config)
    starts = [c["params"] for c in calls if c.get("method") == "thread/start"]
    assert starts[0]["dynamicTools"] and not starts[1]["dynamicTools"]
    assert starts[1]["ephemeral"]
    turns = [c["params"] for c in calls if c.get("method") == "turn/start"]
    summary = json.loads(turns[1]["input"][0]["text"])
    assert [m["content"] for m in summary if m["role"] == "tool"] == [m.content for m in receipts]
    assert "未完成" in summary[-1]["content"]
    cursor = NativeConversation(budget_config.data_dir, response.session_id, session.messages)
    assert json.loads(cursor.path.read_text())["status"] == "pending"

    # The interrupted vendor turn is not reused after a published summary.
    # Normal application history remains visible and reaches the new thread.
    monkeypatch.setenv("FAKE_TOOL_COUNT", "1")
    restarted = AgentLoop.from_config(budget_config)
    restarted.run_turn("继续解释已有数据", session_id=response.session_id)
    calls = protocol(budget_config)
    assert not any(c.get("method") == "thread/resume" for c in calls)
    latest = [c for c in calls if c.get("method") == "turn/start"][-1]
    history = json.loads(latest["params"]["input"][0]["text"])
    assert any(m["content"] == response.summary for m in history)
    assert len([m for m in history if m["role"] == "tool"]) == 2
    assert json.loads(cursor.path.read_text())["status"] == "ready"


def test_tool_budget_never_replays_a_completed_mutation(budget_config, monkeypatch):
    monkeypatch.setenv("FAKE_TOOL_COUNT", "3")
    monkeypatch.setenv("FAKE_TOOL", "record_receipt")
    writes = []
    result = NativeAgentClient(budget_config).run_agent(
        [ChatMessage(role="user", content="执行已授权操作后汇总")],
        tools=[{"name": "record_receipt", "parameters": {"type": "object"}}],
        execute_tool=lambda tc: writes.append(tc.id) or json.dumps({"record_id": tc.id}),
    )
    assert result.content == "原生回答"
    assert writes == ["call-test", "call-test-2"]


def test_tool_time_budget_reserves_summary_inside_parent_deadline(budget_config, monkeypatch):
    monkeypatch.setenv("FAKE_STALL_TOOLS", "1")
    started = time.monotonic()
    with llm_request_deadline(started + 1.2):
        result = NativeAgentClient(budget_config).run_agent(
            [ChatMessage(role="user", content="查询并说明没有取得的数据")],
            tools=[{"name": "get_bars"}], execute_tool=lambda tc: pytest.fail("No tool was requested"),
        )
    assert result.content == "原生回答"
    assert time.monotonic() - started < 1.5
    assert len([c for c in protocol(budget_config) if c.get("method") == "turn/start"]) == 2


def test_cancellation_does_not_start_budget_summary(budget_config, monkeypatch):
    monkeypatch.setenv("FAKE_STALL_TOOLS", "1")
    config = replace(budget_config, llm=replace(budget_config.llm, agent_timeout=10))
    with pytest.raises(TurnInterruptedError):
        NativeAgentClient(config).run_agent(
            [ChatMessage(role="user", content="查询")], tools=[{"name": "get_bars"}],
            execute_tool=lambda tc: "{}", is_cancelled=lambda: started_turns(config) == 1,
        )
    assert len([c for c in protocol(budget_config) if c.get("method") == "turn/start"]) == 1


def test_failed_summary_does_not_publish_success_or_resume_pending_turn(budget_config, monkeypatch):
    monkeypatch.setenv("FAKE_TOOL_COUNT", "3")
    monkeypatch.setenv("FAKE_FAIL_SUMMARY", "1")
    events = []
    loop = AgentLoop.from_config(budget_config, on_event=events.append)
    session = loop.session_store.create()
    with pytest.raises(AgentUnavailableError):
        loop.run_turn("查询行情", session_id=session.session_id)
    saved = loop.session_store.load(session.session_id)
    assert len([m for m in saved.messages if m.role == "tool"]) == 2
    assert not any(e.event == "done" for e in events)
    cursor = NativeConversation(budget_config.data_dir, session.session_id, saved.messages)
    assert json.loads(cursor.path.read_text())["status"] == "pending"


def test_registry_preserves_cancellation_and_parent_deadline(budget_config, monkeypatch):
    registry = AgentLoop.from_config(budget_config)._tools

    def interrupted(*args):
        raise TurnInterruptedError()

    monkeypatch.setattr(registry, "_execute", interrupted)
    with pytest.raises(TurnInterruptedError):
        registry.execute("get_bars", "{}")

    def timed_out(*args):
        time.sleep(.03)
        raise TimeoutError("provider timed out")

    monkeypatch.setattr(registry, "_execute", timed_out)
    assert "provider timed out" in registry.execute("get_bars", "{}")
    with llm_request_deadline(time.monotonic() + .01), pytest.raises(TimeoutError):
        registry.execute("get_bars", "{}")


def test_callback_deadline_retains_unknown_outcome_and_summary_reserve(budget_config, monkeypatch):
    writes, interrupted = [], []
    monkeypatch.setenv("FAKE_TOOL", "record_receipt")
    config = replace(budget_config, llm=replace(budget_config.llm, agent_timeout=1.2))

    def execute(tc):
        writes.append(tc.id)
        while True:
            time.sleep(.01)
            check_execution_active()

    started = time.monotonic()
    result = NativeAgentClient(config).run_agent(
        [ChatMessage(role="user", content="执行并汇总")], tools=[{"name": "record_receipt"}],
        execute_tool=execute, on_tool_interrupted=lambda tc, value: interrupted.append(json.loads(value)),
    )
    assert time.monotonic() - started < 1.5
    assert writes == ["call-test"] and result.incomplete_reason
    assert interrupted[0]["execution_unconfirmed"] is True
    assert result.content == "原生回答"


def test_user_cancellation_reaches_nested_model_without_late_mutation(budget_config, monkeypatch):
    writes = []
    config = replace(budget_config, llm=replace(budget_config.llm, agent_timeout=10))

    def cancelled():
        # Cancel at the observed nested request, not during process startup on
        # a busy machine. The configured deadlines still bound a broken run.
        return started_turns(config) == 2

    def execute(tc):
        monkeypatch.setenv("FAKE_STALL", "1")
        NativeAgentClient(config).complete([ChatMessage(role="user", content="专家研究")])
        writes.append(tc.id)
        return "{}"

    with pytest.raises(TurnInterruptedError):
        NativeAgentClient(config).run_agent(
            [ChatMessage(role="user", content="查询")], tools=[{"name": "get_bars"}], execute_tool=execute,
            is_cancelled=cancelled,
        )
    assert not writes
    assert len([c for c in protocol(budget_config) if c.get("method") == "turn/start"]) == 2


def test_single_call_budget_and_unknown_failure_are_distinct(budget_config, monkeypatch):
    monkeypatch.setenv("FAKE_TOOL_COUNT", "2")
    writes = []
    client = NativeAgentClient(replace(budget_config, llm=replace(budget_config.llm, native_max_tool_calls=1)))
    result = client.run_agent([ChatMessage(role="user", content="查询")], tools=[{"name": "get_bars"}],
                             execute_tool=lambda tc: writes.append(tc.id) or "{}")
    assert writes == ["call-test"] and result.incomplete_reason
    previous = len([c for c in protocol(budget_config) if c.get("method") == "turn/start"])

    def invalid(tc):
        raise ValueError("unexpected execution failure")

    with pytest.raises(ValueError):
        client.run_agent([ChatMessage(role="user", content="查询")], tools=[{"name": "get_bars"}], execute_tool=invalid)
    assert len([c for c in protocol(budget_config) if c.get("method") == "turn/start"]) == previous + 1


def test_incomplete_specialist_result_is_not_learned_as_complete(budget_config, monkeypatch):
    monkeypatch.setenv("FAKE_TOOL", "dispatch_specialists")
    loop = AgentLoop.from_config(budget_config)
    monkeypatch.setattr(loop._tools, "_execute", lambda *args: json.dumps({"results": [{"output": json.dumps({
        "report_markdown": "部分结果", "incomplete_reason": "专家任务未全部完成：工具预算已用尽。"})}]}))
    monkeypatch.setattr("trade_compass_agent.runtime.loop.curate_turn_insight",
                        lambda **kw: pytest.fail("Partial research must not enter automatic learning"))
    response = loop.run_turn("研究走势")
    assert response.degraded
    assert any("专家任务未全部完成" in warning for warning in response.warnings)


def test_http_turn_exposes_partial_completion(budget_config, monkeypatch, client):
    monkeypatch.setenv("FAKE_TOOL_COUNT", "3")
    monkeypatch.setattr("trade_compass_agent.web.agent_api.load_app_config", lambda: budget_config)
    result = client.post("/api/agent/turn", json={"message": "查询行情"})
    assert result.status_code == 200
    body = result.json()
    assert body["degraded"] is True
    assert any("工具预算" in warning for warning in body["warnings"])
    assert "任务未全部完成" in body["summary"]
