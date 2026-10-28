import json
from unittest.mock import Mock

import pytest

from trade_compass_agent.config import AgentConfig, AppConfig, LLMConfig
from trade_compass_agent.llm.providers import ChatCompletion, ChatMessage, OpenAIChatClient, ToolCall
from trade_compass_agent.runtime.exceptions import AgentTurnError
from trade_compass_agent.runtime.session import SessionMessageRecord


def test_context_error_never_restarts_a_turn_or_replays_completed_mutations(tmp_path, monkeypatch):
    from trade_compass_agent.runtime.loop import AgentLoop

    class Client:
        name = model = "mock"
        calls = 0

        def stream_complete(self, messages, **kwargs):
            self.calls += 1
            if self.calls in {1, 3}:
                return ChatCompletion(content=None, tool_calls=[ToolCall(
                    id=f"effect-{self.calls}", name="write_memory",
                    arguments='{"scope":"notes","content":"record once"}')], model="mock", provider="mock")
            if self.calls == 2:
                raise AgentTurnError("maximum context length exceeded")
            return ChatCompletion(content="finished", model="mock", provider="mock")

        complete = stream_complete

    client = Client()
    monkeypatch.setattr("trade_compass_agent.runtime.loop.create_chat_client", lambda config: client)
    config = AppConfig(data_dir=tmp_path/"data", memory_dir=tmp_path/"memory", data_provider="sample",
                       llm=LLMConfig(provider="disabled"), agent=AgentConfig(learning_enabled=False))
    agent = AgentLoop.from_config(config)
    session = agent.session_store.get_or_create("context-replay")
    for index in range(8):
        agent.session_store.append(session, SessionMessageRecord(role="user", content=f"old {index}"))
        agent.session_store.append(session, SessionMessageRecord(role="assistant", content="", tool_calls=[{
            "id": f"old-{index}", "function": {"name":"search_memory", "arguments":"{}"}}]))
        agent.session_store.append(session, SessionMessageRecord(role="tool", name="search_memory",
            tool_call_id=f"old-{index}", content=json.dumps({"text":"old evidence "*500})))
    effects=[]

    def execute(name, arguments):
        if name == "write_memory":
            effects.append(arguments)
        return json.dumps({"ok":True,"text":"saved evidence "*500})

    monkeypatch.setattr(agent._tools, "execute", execute)
    with pytest.raises(AgentTurnError, match="context length"):
        agent.run_turn("记录本次经验，只写一次。", session_id=session.session_id)
    assert len(effects)==1
    assert client.calls==2
    stored=agent.session_store.load(session.session_id)
    assert sum(m.role=="user" and m.content=="记录本次经验，只写一次。" for m in stored.messages)==1


def test_lmstudio_reads_loaded_instance_window_not_architectural_max(monkeypatch):
    from trade_compass_agent.llm import providers

    response=Mock()
    response.json.return_value={"models":[{"key":"qwen3.8-27b","max_context_length":262144,
        "loaded_instances":[{"id":"qwen3.8-27b","config":{"context_length":131072}}]}]}
    monkeypatch.setattr("httpx.get", lambda *a,**k: response)
    assert providers._lmstudio_loaded_context_window("http://localhost:1234/v1", "qwen3.8-27b", "unit")==131072


def test_loaded_window_reserves_thinking_output_even_with_larger_config():
    from trade_compass_agent.runtime.compression.budget import TokenBudget

    budget=TokenBudget(AppConfig(), context_window=32768, output_reserve=16384)
    assert budget.context_budget==16384


def test_large_followup_is_rejected_before_any_sdk_generation(monkeypatch):
    from trade_compass_agent.llm import providers

    monkeypatch.setattr(providers, "_lmstudio_loaded_context_window", lambda *a,**k:32768)
    client=OpenAIChatClient(name="lmstudio",model="qwen3.8-27b",api_key="unit",
        base_url="http://127.0.0.1:9/v1",discover_context=True)
    sdk=Mock(side_effect=AssertionError("oversize requests must not reach the model"))
    monkeypatch.setattr(client._client.chat.completions,"create",sdk)
    client._validate_context_budget({"messages":[{"role":"user","content":"small"}],"max_tokens":16384})
    with pytest.raises(AgentTurnError,match="上下文"):
        client.stream_complete([ChatMessage(role="user",content="证据"*20000)])
    sdk.assert_not_called()


@pytest.mark.parametrize("window,expected", [(32768,16384), (65536,32768), (131072,32768)])
def test_thinking_output_has_room_for_a_complete_answer_without_overcommitting_small_windows(monkeypatch, window, expected):
    from trade_compass_agent.llm import providers

    monkeypatch.setattr(providers, "_lmstudio_loaded_context_window", lambda *a,**k:window)
    client=OpenAIChatClient(name="lmstudio",model="qwen3.8-27b",api_key="unit",
        base_url="http://127.0.0.1:9/v1",discover_context=True)
    assert client.response_token_limit==expected
    captured=[]
    monkeypatch.setattr(client, "_stream_api", lambda kwargs,**k:captured.append(kwargs) or ChatCompletion(content="done"))
    client.stream_complete([ChatMessage(role="user",content="review")])
    assert captured[-1]["max_tokens"]==expected
    client.complete_raw([{"role":"user","content":"review"}])
    assert captured[-1]["max_tokens"]==expected
    with providers.llm_maintenance_mode():
        client.stream_complete([ChatMessage(role="user",content="summary")])
    assert captured[-1]["max_tokens"]==4096
