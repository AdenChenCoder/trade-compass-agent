"""Keep Qwen3.8 tool-round reasoning transient, budgeted, and off the transcript."""

import json
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest

from trade_compass_agent.config import AgentConfig, AppConfig, LLMConfig
from trade_compass_agent.llm.providers import (
    ChatCompletion,
    ChatMessage,
    OpenAIChatClient,
    ToolCall,
)
from trade_compass_agent.runtime.compression.budget import estimate_message_tokens, estimate_tokens
from trade_compass_agent.runtime.exceptions import AgentTurnError


PRIVATE = "PRIVATE_REASONING_CONTEXT_FIXTURE"


def test_transient_reasoning_is_not_in_default_diagnostic_representations():
    assert PRIVATE not in repr(ChatMessage(role="assistant", reasoning_content=PRIVATE))
    assert PRIVATE not in repr(ChatCompletion(content="public", reasoning_content=PRIVATE))


@pytest.mark.parametrize(
    "provider,model,preserve",
    [
        ("lmstudio", "qwen3.8-27b", True),
        ("lmstudio", "qwen3.6-35b-a3b", True),
        ("lmstudio", "qwen3.5-35b-a3b", False),
        ("openai", "qwen3.8-27b", False),
    ],
)
def test_reasoning_roundtrip_uses_real_sdk_wire_only_for_supported_local_model(
    tmp_path,
    monkeypatch,
    provider,
    model,
    preserve,
):
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        deltas = (
            [
                {"reasoning_content": PRIVATE[:12]},
                {"reasoning": PRIVATE[12:]},
                {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "read",
                            "type": "function",
                            "function": {"name": "read_test", "arguments": "{}"},
                        }
                    ]
                },
            ]
            if len(requests) == 1
            else [{"content": "done"}]
        )
        chunks = [
            {
                "id": "fixture",
                "model": model,
                "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
            }
            for delta in deltas
        ]
        chunks.append(
            {
                "id": "fixture",
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "delta": {},
                        "finish_reason": "tool_calls" if len(requests) == 1 else "stop",
                    }
                ],
            }
        )
        body = (
            "".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks) + "data: [DONE]\n\n"
        )
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    monkeypatch.setattr(
        "trade_compass_agent.llm.providers._usage_http_client",
        lambda: httpx.Client(transport=httpx.MockTransport(handler)),
    )
    client = OpenAIChatClient(
        name=provider,
        model=model,
        api_key="unit",
        base_url="http://localhost:1234/v1",
        usage_db_path=tmp_path / "usage.db",
    )
    result = client.stream_complete([ChatMessage(role="user", content="inspect")])
    assert getattr(result, "reasoning_content", "") == (PRIVATE if preserve else "")
    assert not result.content and result.tool_calls[0].name == "read_test"
    history = [
        ChatMessage(role="user", content="inspect"),
        ChatMessage(
            role="assistant",
            reasoning_content=PRIVATE,
            tool_calls=[
                {
                    "id": "read",
                    "type": "function",
                    "function": {"name": "read_test", "arguments": "{}"},
                }
            ],
        ),
        ChatMessage(role="tool", tool_call_id="read", name="read_test", content="fact"),
    ]
    assert client.stream_complete(history).content == "done"
    assert requests[1]["messages"][1].get("reasoning_content") == (PRIVATE if preserve else None)


def test_reasoning_is_counted_and_oversized_followup_never_reaches_sdk(monkeypatch):
    from trade_compass_agent.llm import providers

    private = PRIVATE + "判断" * 20000
    message = ChatMessage(role="assistant", content="public", reasoning_content=private)
    assert estimate_message_tokens(message) >= estimate_tokens(private)
    monkeypatch.setattr(providers, "_lmstudio_loaded_context_window", lambda *a, **k: 32768)
    client = OpenAIChatClient(
        name="lmstudio",
        model="qwen3.8-27b",
        api_key="unit",
        base_url="http://127.0.0.1:9/v1",
        discover_context=True,
    )
    request = Mock(side_effect=AssertionError("Oversize reasoning must not reach model"))
    monkeypatch.setattr(client._client.chat.completions, "create", request)
    with pytest.raises(AgentTurnError, match="上下文") as error:
        client.stream_complete([message])
    assert PRIVATE not in str(error.value)
    request.assert_not_called()


def test_reasoning_does_not_spend_the_recent_source_protection_quota():
    from dataclasses import replace
    from trade_compass_agent.runtime.compression.trim import _resolve_protect_tail_boundary

    messages = [ChatMessage(role="user", content="source evidence " * 8) for _ in range(6)]
    messages.extend(
        [
            ChatMessage(role="assistant", content="inspect", reasoning_content=PRIVATE * 200),
            ChatMessage(role="tool", content="latest verified fact"),
        ]
    )
    public = [replace(message, reasoning_content="") for message in messages]
    assert _resolve_protect_tail_boundary(messages, 0, 100) == _resolve_protect_tail_boundary(
        public, 0, 100
    )
    assert sum(estimate_message_tokens(m) for m in messages) > sum(
        estimate_message_tokens(m) for m in public
    )


def test_factual_summary_input_excludes_transient_reasoning():
    from trade_compass_agent.runtime.compression.summarizer import _build_summarize_user_message

    prompt = _build_summarize_user_message(
        [
            ChatMessage(role="assistant", content="public conclusion", reasoning_content=PRIVATE),
            ChatMessage(role="tool", content="verified source fact"),
        ]
    )
    assert PRIVATE not in prompt
    assert "public conclusion" in prompt and "verified source fact" in prompt


def test_tool_round_keeps_reasoning_but_history_and_next_turn_do_not(tmp_path, monkeypatch):
    from trade_compass_agent.runtime import loop
    from trade_compass_agent.runtime.tools.registry import ToolRegistry

    requests = []

    def stream(messages, **kwargs):
        requests.append(list(messages))
        if len(requests) == 1:
            return ChatCompletion(
                content=None,
                reasoning_content=PRIVATE,
                tool_calls=[ToolCall("portfolio", "analyze_portfolio", "{}")],
            )
        if len(requests) == 2:
            assistant = next(m for m in messages if m.tool_calls)
            assert assistant.reasoning_content == PRIVATE
            return ChatCompletion(content="当前没有持仓，本轮仅查询，没有成交。")
        assert all(not m.reasoning_content for m in messages)
        return ChatCompletion(content="前轮仅查询，没有进行交易。")

    monkeypatch.setattr(
        loop, "create_chat_client", lambda *_: SimpleNamespace(stream_complete=stream)
    )
    monkeypatch.setattr(loop, "shared_consolidation_worker", lambda **_: None)
    monkeypatch.setattr(
        ToolRegistry, "execute", lambda *a, **k: json.dumps({"positions": [], "accounts": []})
    )
    config = AppConfig(
        data_dir=tmp_path / "data",
        memory_dir=tmp_path / "memory",
        data_provider="sample",
        llm=LLMConfig(provider="disabled"),
        agent=AgentConfig(learning_enabled=False, llm_session_titles=False),
    )
    agent = loop.AgentLoop.from_config(config)
    assert "没有成交" in agent.run_turn("只查询持仓", session_id="reasoning-context").summary
    for path in config.data_dir.rglob("*.jsonl"):
        assert PRIVATE not in path.read_text()
    assert "仅查询" in agent.run_turn("复述前轮结论", session_id="reasoning-context").summary
    assert len(requests) == 3
