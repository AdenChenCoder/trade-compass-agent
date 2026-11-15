import asyncio
import json

import pytest

from trade_compass_agent.llm.lmstudio_context import ContextMeasurement, measure_context


def fake_rpc(monkeypatch, *, loaded=True, delay=0, window=131072, fault=None):
    from trade_compass_agent.llm import lmstudio_context as counter
    seen = {"closed": 0, "loads": 0, 'endpoints': [], 'public_texts': []}
    class Client:
        def __init__(self, **kwargs):
            assert kwargs == {'trust_env': False}
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            seen["closed"] += 1
    class Websocket:
        def __init__(self, url, **kwargs):
            seen['host'] = url
            assert kwargs['max_message_size_bytes'] == 8 * 1024 * 1024
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            seen['socket_closed'] = True
        async def send_json(self, message):
            self.message = message
        async def receive_json(self):
            message = self.message
            if 'authVersion' in message:
                return {'success': fault != 'auth'}
            endpoint = message['endpoint']
            seen['endpoints'].append(endpoint)
            parameter = message.get('parameter', {})
            if endpoint == 'listLoaded':
                value = [{'identifier': 'qwen3.8-27b'}] if loaded else []
            elif endpoint == 'getModelInfo':
                value = {'contextLength': window}
            elif endpoint == 'applyPromptTemplate':
                seen['history'], seen['opts'] = parameter['history'], parameter['opts']
                seen['formatted'] = json.dumps(parameter['history']) + json.dumps(parameter['opts'])
                value = {'formatted': seen['formatted']}
            elif endpoint == 'countTokens':
                text = parameter['inputString']
                if 'total' not in seen:
                    await asyncio.sleep(delay)
                    seen['total'] = text
                    count = -1 if fault == 'count' else 123
                else:
                    seen['public_texts'].append(text)
                    count = len(text)
                value = {'tokenCount': count}
            else:
                raise AssertionError('No generation or model lifecycle operation is allowed')
            return {'type': 'rpcError' if fault == 'error' else 'rpcResult',
                    'callId': message['callId'] + int(fault == 'id'), 'result': value}
    monkeypatch.setattr(counter, 'AsyncClient', Client)
    monkeypatch.setattr(counter, 'aconnect_ws', Websocket)
    return seen


def test_native_counter_includes_template_tools_and_private_reasoning_but_protects_public_sources(
    monkeypatch,
):
    seen = fake_rpc(monkeypatch)
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
    seen = fake_rpc(monkeypatch, delay=0.2)
    assert (
        measure_context(
            "http://localhost:1234/v1", "qwen3.8-27b", {"prompt": "facts"}, timeout=0.01
        )
        is None
    )
    assert seen["closed"] == 1


def test_missing_model_never_triggers_implicit_load(monkeypatch):
    seen = fake_rpc(monkeypatch, loaded=False)
    assert measure_context("http://localhost:1234/v1", "qwen3.8-27b", {"prompt": "facts"}) is None
    assert seen["loads"] == 0 and seen["closed"] == 1


@pytest.mark.parametrize('fault', ['auth', 'error', 'id', 'count'])
def test_counter_protocol_failure_keeps_safe_fallback_and_closes(monkeypatch, fault):
    seen = fake_rpc(monkeypatch, fault=fault)
    assert measure_context('http://localhost:1234/v1', 'qwen3.8-27b', {'prompt': 'facts'}) is None
    assert seen['closed'] == 1 and seen['socket_closed']
    assert set(seen['endpoints']) <= {'listLoaded', 'getModelInfo', 'applyPromptTemplate', 'countTokens'}


@pytest.mark.parametrize('frame_limit,supported', [(65536, False), (8 * 1024 * 1024, True)])
def test_large_template_frame_uses_complete_input_without_sdk_receive_ceiling(monkeypatch, frame_limit, supported):
    import httpx
    from httpx_ws.transport import ASGIWebSocketTransport
    from starlette.applications import Starlette
    from starlette.routing import WebSocketRoute
    from starlette.websockets import WebSocketDisconnect
    from trade_compass_agent.llm import lmstudio_context as counter

    text = 'source-data-0123456789' * 4000
    formatted = 'native-template:' + text
    seen = {'endpoints': [], 'counted': [], 'closed': False}
    async def server(websocket):
        await websocket.accept()
        auth = await websocket.receive_json()
        assert set(auth) == {'authVersion', 'clientIdentifier', 'clientPasskey'}
        await websocket.send_json({'success': True})
        try:
            while True:
                request = await websocket.receive_json()
                endpoint = request['endpoint']
                seen['endpoints'].append(endpoint)
                parameter = request.get('parameter', {})
                if endpoint == 'listLoaded':
                    result = [{'identifier': 'framed-counter'}]
                elif endpoint == 'getModelInfo':
                    result = {'contextLength': 131072}
                elif endpoint == 'applyPromptTemplate':
                    assert parameter['history']['messages'][0]['content'][0]['text'] == text
                    result = {'formatted': formatted}
                elif endpoint == 'countTokens':
                    seen['counted'].append(parameter['inputString'])
                    result = {'tokenCount': 321}
                else:
                    raise AssertionError('This is an explicit counter double, never a model executor')
                await websocket.send_json({'type': 'rpcResult', 'callId': request['callId'], 'result': result})
        except WebSocketDisconnect:
            pass
        finally:
            seen['closed'] = True
    app = Starlette(routes=[WebSocketRoute('/llm', server)])
    monkeypatch.setattr(counter, 'AsyncClient', lambda **kwargs:
        httpx.AsyncClient(transport=ASGIWebSocketTransport(app), **kwargs))
    connect = counter.aconnect_ws
    def framed(*args, **kwargs):
        assert kwargs['max_message_size_bytes'] == 8 * 1024 * 1024
        kwargs['max_message_size_bytes'] = frame_limit
        return connect(*args, **kwargs)
    monkeypatch.setattr(counter, 'aconnect_ws', framed)
    async def run():
        try:
            return await asyncio.wait_for(counter._measure('localhost:9', 'framed-counter', {
                'messages': [{'role': 'user', 'content': text}]}), 2)
        except Exception:
            return None
    result = asyncio.run(run())
    assert (result is not None) is supported
    if supported:
        assert result == ContextMeasurement(321, 131072, (325,))
        assert seen['counted'] == [formatted, text]
    assert seen['closed']
    assert set(seen['endpoints']) <= {'listLoaded', 'getModelInfo', 'applyPromptTemplate', 'countTokens'}


def test_unknown_window_is_not_replaced_with_architectural_maximum(monkeypatch):
    seen = fake_rpc(monkeypatch, window=-1)
    assert measure_context("http://localhost:1234/v1", "qwen3.8-27b", {"prompt": "facts"}) is None
    assert seen["closed"] == 1 and seen["loads"] == 0


def test_remote_keeps_safe_fallback(monkeypatch):
    seen = fake_rpc(monkeypatch)
    assert measure_context("https://remote.example/v1", "qwen3.8-27b", {"prompt": "facts"}) is None
    assert "host" not in seen


def test_existing_event_loop_still_uses_the_native_counter(monkeypatch):
    seen = fake_rpc(monkeypatch)

    async def running():
        return measure_context("http://localhost:1234/v1", "qwen3.8-27b", {"prompt": "facts"})

    result = asyncio.run(running())
    assert result.input_tokens == 123 and result.native
    assert seen["closed"] == 1 and seen["loads"] == 0


def test_existing_loop_counter_deadline_does_not_wait_for_the_worker(monkeypatch):
    import time
    seen = fake_rpc(monkeypatch, delay=0.2)

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
