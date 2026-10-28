import asyncio
import json
import sys
from types import SimpleNamespace

import pytest

from trade_compass_agent.llm.lmstudio_context import ContextMeasurement, measure_context


def fake_sdk(monkeypatch, *, loaded=True, delay=0, window=131072):
    seen = {"closed": 0, "loads": 0}

    class Model:
        identifier = "qwen3.8-27b"

        async def apply_prompt_template(self, history, opts):
            seen["history"], seen["opts"] = history, opts
            return json.dumps(history) + json.dumps(opts)

        async def tokenize(self, texts):
            seen["public_texts"] = texts
            return [[1] * len(text) for text in texts]

        async def count_tokens(self, text):
            await asyncio.sleep(delay)
            return 123

        async def get_context_length(self):
            return window

    class Client:
        def __init__(self, **kwargs):
            seen["host"] = kwargs["api_host"]
            self.llm = self

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            seen["closed"] += 1

        async def list_loaded(self):
            return [Model()] if loaded else []

        async def model(self, *a, **k):
            seen["loads"] += 1
            raise AssertionError("Counting must not load a model")

    monkeypatch.setitem(sys.modules, "lmstudio", SimpleNamespace(AsyncClient=Client))
    return seen


def test_native_counter_includes_template_tools_and_private_reasoning_but_protects_public_sources(
    monkeypatch,
):
    seen = fake_sdk(monkeypatch)
    tool = {"type": "function", "function": {"name": "read", "parameters": {"type": "object"}}}
    request = {
        "messages": [
            {"role": "user", "content": "task"},
            {
                "role": "assistant",
                "content": "",
                "reasoning_content": "PRIVATE",
                "tool_calls": [{"id": "one", "function": {"name": "read", "arguments": "{}"}}],
            },
            {"role": "tool", "tool_call_id": "one", "content": "verified source"},
        ],
        "tools": [tool],
    }
    result = measure_context("http://127.0.0.1:1234/v1", "qwen3.8-27b", request)
    assert result.input_tokens == 123 and result.context_window == 131072
    assert seen["opts"]["toolDefinitions"] == [tool]
    assert "PRIVATE" in json.dumps(seen["history"])
    assert "PRIVATE" not in "".join(seen["public_texts"])
    assert result.public_message_tokens[-1] == len("verified source") + 4
    assert seen["loads"] == 0 and seen["closed"] == 1


def test_counter_deadline_closes_client_and_returns_safe_fallback(monkeypatch):
    seen = fake_sdk(monkeypatch, delay=0.2)
    assert (
        measure_context(
            "http://localhost:1234/v1", "qwen3.8-27b", {"prompt": "facts"}, timeout=0.01
        )
        is None
    )
    assert seen["closed"] == 1


def test_missing_model_never_triggers_implicit_load(monkeypatch):
    seen = fake_sdk(monkeypatch, loaded=False)
    assert measure_context("http://localhost:1234/v1", "qwen3.8-27b", {"prompt": "facts"}) is None
    assert seen["loads"] == 0 and seen["closed"] == 1


def test_unknown_window_is_not_replaced_with_architectural_maximum(monkeypatch):
    seen = fake_sdk(monkeypatch, window=-1)
    assert measure_context("http://localhost:1234/v1", "qwen3.8-27b", {"prompt": "facts"}) is None
    assert seen["closed"] == 1 and seen["loads"] == 0


def test_remote_keeps_safe_fallback(monkeypatch):
    seen = fake_sdk(monkeypatch)
    assert measure_context("https://remote.example/v1", "qwen3.8-27b", {"prompt": "facts"}) is None
    assert "host" not in seen


def test_existing_event_loop_still_uses_the_native_counter(monkeypatch):
    seen = fake_sdk(monkeypatch)

    async def running():
        return measure_context("http://localhost:1234/v1", "qwen3.8-27b", {"prompt": "facts"})

    result = asyncio.run(running())
    assert result.input_tokens == 123 and result.native
    assert seen["closed"] == 1 and seen["loads"] == 0


def test_existing_loop_counter_deadline_does_not_wait_for_the_worker(monkeypatch):
    import time
    seen = fake_sdk(monkeypatch, delay=0.2)

    async def running():
        started = time.monotonic()
        result = measure_context("http://localhost:1234/v1", "qwen3.8-27b", {"prompt": "facts"}, timeout=0.01)
        assert result is None and time.monotonic() - started < 0.15
        await asyncio.sleep(0.05)

    asyncio.run(running())
    assert seen["closed"] == 1 and seen["loads"] == 0


def test_provider_guard_uses_native_count_instead_of_false_character_overflow(monkeypatch):
    from trade_compass_agent.llm import providers
    from trade_compass_agent.runtime.exceptions import LLMContextBudgetError

    monkeypatch.setattr(providers, "_lmstudio_loaded_context_window", lambda *a: 131072)
    measurement = ContextMeasurement(60000, 131072, (100,))
    monkeypatch.setattr(providers, "_measure_lmstudio_context", lambda *a, **k: measurement)
    client = providers.OpenAIChatClient(
        name="lmstudio",
        model="qwen3.8-27b",
        api_key="unit",
        base_url="http://localhost:1234/v1",
        discover_context=True,
    )
    kwargs = {"messages": [{"role": "user", "content": "123456," * 20000}], "max_tokens": 32768}
    client._validate_context_budget(kwargs)
    assert (
        client.measure_input_tokens(
            [providers.ChatMessage(role="user", content="facts")]
        ).input_tokens
        == 60512
    )
    measurement = ContextMeasurement(100000, 131072)
    with pytest.raises(LLMContextBudgetError):
        client._validate_context_budget(kwargs)


def test_native_count_honors_earlier_parent_deadline(monkeypatch):
    import time
    from trade_compass_agent.llm import providers

    captured = []
    monkeypatch.setattr(providers, "_lmstudio_loaded_context_window", lambda *a: 131072)
    monkeypatch.setattr(
        providers,
        "_measure_lmstudio_context",
        lambda *a, **k: captured.append(k["timeout"]) or ContextMeasurement(10, 131072),
    )
    client = providers.OpenAIChatClient(
        name="lmstudio",
        model="qwen3.8-27b",
        api_key="unit",
        base_url="http://localhost:1234/v1",
        discover_context=True,
    )
    with providers.llm_request_deadline(time.monotonic() + 0.5):
        client.measure_input_tokens([providers.ChatMessage(role="user", content="facts")])
    assert 0 < captured[0] <= 0.5


def test_multimodal_compaction_never_counts_image_encoding_as_text(monkeypatch):
    from trade_compass_agent.llm.providers import ChatMessage, OpenAIChatClient

    client = OpenAIChatClient(name="lmstudio", model="qwen3.8-27b", api_key="unit",
                              base_url="http://localhost:9/v1")

    def unexpected(*args, **kwargs):
        raise AssertionError("Images need their existing image-aware budget path")

    monkeypatch.setattr(client, "_measure_input_context", unexpected)
    assert client.measure_input_tokens([ChatMessage(role="user", content=[
        {"type": "image_url", "image_url": {"url": "data:image/png;base64," + "A" * 100000}},
    ])]) is None
