"""Consumer checks for the local model's input and output allocation."""

import pytest
import yaml

from trade_compass_agent.config import (
    AppConfig,
    CompressionConfig,
    PROJECT_ROOT,
    invalidate_config_cache,
)
from trade_compass_agent.llm.providers import ChatCompletion, ChatMessage
from trade_compass_agent.runtime.compression.budget import TokenBudget
from trade_compass_agent.runtime.compression.summarizer import summarize_middle_turns
from trade_compass_agent.runtime.compression.trim import _resolve_protect_tail_boundary
from trade_compass_agent.runtime.context import ContextBuilder


def _compression_defaults(name="default.yaml"):
    raw = yaml.safe_load((PROJECT_ROOT / "config" / name).read_text())
    return CompressionConfig(**raw["context_compression"])


@pytest.mark.parametrize("name", ["default.yaml", "packaged.yaml"])
def test_defaults_allocate_96k_input_and_leave_32k_output(name):
    compression = _compression_defaults(name)
    budget = TokenBudget(
        AppConfig(context_compression=compression),
        context_window=131072,
        output_reserve=32768,
    )
    assert budget.context_budget == 98304
    assert compression.protect_recent_tokens == 65536
    assert not budget.should_summarize(32768)
    threshold = int(98304 * compression.summary_threshold_pct)
    assert not budget.should_summarize(threshold - 1)
    assert budget.should_summarize(threshold)
    smaller_window = TokenBudget(
        AppConfig(context_compression=compression),
        context_window=32768,
        output_reserve=16384,
    )
    assert smaller_window.context_budget == 16384


@pytest.mark.parametrize("tokens,expected", [(0, 80), (50, 80), (150, 70), (500, 0)])
def test_tail_protection_uses_the_larger_message_or_token_allowance(tokens, expected):
    messages = [ChatMessage(role="user", content="hello") for _ in range(100)]
    assert _resolve_protect_tail_boundary(messages, 20, tokens) == expected


def test_chinese_tail_protection_uses_the_same_estimate_as_preflight():
    messages = [ChatMessage(role="user", content="证据" * 3000) for _ in range(20)]
    assert _resolve_protect_tail_boundary(messages, 2, 8009) == 18


def test_digit_dense_financial_input_cannot_spend_the_reserved_output_window(monkeypatch):
    from unittest.mock import Mock

    from trade_compass_agent.llm import providers
    from trade_compass_agent.runtime.exceptions import LLMContextBudgetError

    monkeypatch.setattr(providers, "_lmstudio_loaded_context_window", lambda *args, **kwargs: 131072)
    client = providers.OpenAIChatClient(
        name="lmstudio", model="qwen3.8-27b", api_key="unit",
        base_url="http://127.0.0.1:9/v1", discover_context=True,
    )
    sdk = Mock(side_effect=AssertionError("Oversized input must not reach generation"))
    monkeypatch.setattr(client._client.chat.completions, "create", sdk)
    # This tokenizer splits digits individually: the six-digit codes alone
    # exceed the 96K input allowance, although len(text) / 4 looks small.
    with pytest.raises(LLMContextBudgetError, match="上下文"):
        client.stream_complete([ChatMessage(role="user", content="600519|" * 18000)])
    sdk.assert_not_called()


def _research_history():
    evidence = "original filing details\n" * 220 + "CORRECTION: net profit is 731, not 915."
    messages = [
        ChatMessage(role="user", content="Read the filing."),
        ChatMessage(role="assistant", content="Checking the source."),
        ChatMessage(role="user", content="Use the corrected figures."),
        ChatMessage(role="assistant", content="", tool_calls=[{
            "id": "filing-1", "function": {"name": "search_web", "arguments": "{}"},
        }]),
        ChatMessage(role="tool", content=evidence, name="search_web", tool_call_id="filing-1"),
    ]
    for index in range(40):
        messages.append(ChatMessage(
            role="user" if index % 2 == 0 else "assistant",
            content=f"Research note {index}: " + "observation " * 500,
        ))
    return messages, evidence


def test_research_beyond_32k_keeps_recent_source_details_after_20_messages(tmp_path):
    history, evidence = _research_history()
    compression = _compression_defaults()
    builder = ContextBuilder(
        memory_dir=tmp_path / "memory", skills=[], compression_config=compression,
    )
    built = builder.build_messages(history, "What is the corrected net profit?")
    filing = next(message for message in built if message.tool_call_id == "filing-1")
    assert filing.content == evidence
    assert history[4].content == evidence
    budget = TokenBudget(
        AppConfig(context_compression=compression), context_window=131072, output_reserve=32768,
    )
    estimated = budget.estimate(built)
    assert 32768 < estimated < int(budget.context_budget * compression.summary_threshold_pct)
    assert not budget.should_summarize(estimated)


def test_summary_preserves_recent_raw_evidence_and_only_compacts_older_material():
    history, evidence = _research_history()
    messages = [ChatMessage(role="system", content="Use verified facts only.")]
    messages += [ChatMessage(role="user", content=f"Old note {i}: " + "x" * 3000) for i in range(40)]
    messages += history
    summary_inputs = []

    def summarize(system, content):
        summary_inputs.append(content)
        return "<summary>Older research notes were reviewed.</summary>"

    compressed, _, count, saved = summarize_middle_turns(
        messages, llm_call=summarize,
        protect_recent_count=20, protect_recent_tokens=65536,
    )
    assert count > 0 and saved > 0
    assert summary_inputs and all(evidence not in text for text in summary_inputs)
    filing = next(message for message in compressed if message.tool_call_id == "filing-1")
    assert filing.content == evidence
    assert messages[-len(history) + 4].content == evidence
    call_index = next(i for i, message in enumerate(compressed) if message.tool_calls)
    assert compressed[call_index + 1].tool_call_id == "filing-1"


def test_compression_change_reaches_next_api_turn_without_changing_history(client, tmp_path, monkeypatch):
    from trade_compass_agent.runtime import loop
    from trade_compass_agent.web import agent_api

    path = tmp_path / "context.yaml"
    raw = {
        "llm": {"provider": "lmstudio", "model": "qwen3.8-27b"},
        "agent": {"learning_enabled": False},
        "context_compression": {"context_budget": 32768},
    }
    path.write_text(yaml.safe_dump(raw))
    monkeypatch.setenv("TRADE_COMPASS_CONFIG", str(path))
    invalidate_config_cache()
    monkeypatch.setattr(agent_api, "_cached_stack", None)
    monkeypatch.setattr(agent_api, "_cached_config", None)
    monkeypatch.setattr(loop, "shared_consolidation_worker", lambda **kwargs: None)

    class Client:
        name = "lmstudio"
        model = "qwen3.8-27b"

        def __init__(self, config):
            self.budget = config.context_compression.context_budget

        def stream_complete(self, messages, **kwargs):
            return ChatCompletion(content=str(self.budget), model=self.model, provider=self.name)

        complete = stream_complete

    monkeypatch.setattr(loop, "create_chat_client", Client)
    try:
        first = client.post("/api/agent/turn", json={"message": "Check context allocation."})
        assert first.status_code == 200
        assert first.json()["summary"] == "32768"
        session_id = first.json()["session_id"]
        raw["context_compression"]["context_budget"] = 98304
        path.write_text(yaml.safe_dump(raw))
        invalidate_config_cache()
        second = client.post("/api/agent/turn", json={
            "message": "Check it again.", "session_id": session_id,
        })
        assert second.status_code == 200
        assert second.json()["summary"] == "98304"
        stored = agent_api._session_store().load(session_id)
        assert [m.content for m in stored.messages if m.role == "user"] == [
            "Check context allocation.", "Check it again.",
        ]
    finally:
        invalidate_config_cache()
