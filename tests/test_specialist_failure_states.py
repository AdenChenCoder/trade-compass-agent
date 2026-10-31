from dataclasses import replace
import json
from types import SimpleNamespace

import pytest

from trade_compass_agent.config import AppConfig
from trade_compass_agent.llm.native import NativeAgentClient
from trade_compass_agent.llm.providers import ChatCompletion, ToolCall
from trade_compass_agent.runtime import loop as loop_module
from trade_compass_agent.runtime.market_stack import MarketStack
from trade_compass_agent.runtime.tools import dispatch


@pytest.mark.parametrize("backend", ["api", "native"])
@pytest.mark.parametrize("count", [1, 2])
@pytest.mark.parametrize("failure", ["exception", "structured", "empty"])
def test_failed_specialist_reaches_http_without_learning(client, tmp_path, monkeypatch, backend, count, failure):
    config = AppConfig(data_dir=tmp_path / "data", memory_dir=tmp_path / "memory", data_provider="sample")
    if backend == "native":
        config = replace(config, llm=replace(config.llm, provider="codex"))
    stack = MarketStack.from_config(config)
    monkeypatch.setattr("trade_compass_agent.web.agent_api._get_cached_stack", lambda: (config, stack))
    tasks = [{"specialist": f"expert-{index}", "task": "核对研究资料"} for index in range(count)]
    call = ToolCall(id="dispatch", name="dispatch_specialists", arguments=json.dumps({"tasks": tasks}))

    def specialist(stack, name, task, **kw):
        if name == "expert-0":
            if failure == "empty":
                return None
            if failure == "exception":
                raise RuntimeError("模型额度不足，专家未返回报告")
            return json.dumps({"error": "模型额度不足，专家未返回报告"}, ensure_ascii=False)
        return "其他专家已返回可用的研究资料。"

    monkeypatch.setattr(dispatch, "run_specialist", specialist)
    monkeypatch.setattr(dispatch, "build_situation_summary", lambda _: "隔离研究上下文")
    learned, reviews, events = [], [], []
    monkeypatch.setattr(loop_module, "curate_turn_insight", lambda **kw: learned.append(kw))
    monkeypatch.setattr(loop_module.AgentLoop, "_maybe_background_review", lambda *a, **kw: reviews.append(kw))

    class ApiClient:
        def __init__(self):
            self.calls = 0

        def stream_complete(self, messages, **kw):
            self.calls += 1
            return ChatCompletion(content=None, tool_calls=[call]) if self.calls == 1 else ChatCompletion(content="以下为已取得的信息。")

    class NativeClient(NativeAgentClient):
        def __init__(self):
            pass

        def run_agent(self, messages, *, tools, execute_tool, **kw):
            execute_tool(call)
            return ChatCompletion(content="以下为已取得的信息。")

    monkeypatch.setattr(loop_module, "create_chat_client", lambda _: NativeClient() if backend == "native" else ApiClient())
    from trade_compass_agent.web import agent_api
    publish = agent_api._publish_stream
    monkeypatch.setattr(agent_api, "_publish_stream", lambda sid, event: (events.append(event), publish(sid, event)))

    response = client.post("/api/agent/turn", json={"session_id": "failed-child", "message": "安排专家核对资料。"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["degraded"] is True
    assert body["warnings"] and "专家" in body["summary"]
    assert not learned and not reviews
    assert events[-1].event == "done" and events[-1].data["degraded"] is True
    visible = client.get("/api/agent/sessions/failed-child/messages").json()
    assert visible["messages"][-1]["content"] == body["summary"]


def test_invalid_dispatch_task_is_partial_not_success(tmp_path):
    stack = SimpleNamespace(config=AppConfig(data_dir=tmp_path / "data", memory_dir=tmp_path / "memory"))
    result = json.loads(dispatch.tool_dispatch_specialists(stack, [{"task": "缺少专家名称"}]))
    assert result.get("incomplete_reason")
    assert result["results"][0]["error"] == "missing specialist name"


@pytest.mark.parametrize("tasks", [None, [], {}])
def test_missing_task_list_is_incomplete(tmp_path, tasks):
    stack = SimpleNamespace(config=AppConfig(data_dir=tmp_path / "data", memory_dir=tmp_path / "memory"))
    result = json.loads(dispatch.tool_dispatch_specialists(stack, tasks))
    assert result["error"] and result["incomplete_reason"]
