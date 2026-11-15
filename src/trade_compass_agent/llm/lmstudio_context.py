"""Count local requests with the loaded model's tokenizer without generating."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse
from uuid import uuid4

from httpx import AsyncClient
from httpx_ws import aconnect_ws


@dataclass(frozen=True)
class ContextMeasurement:
    input_tokens: int
    context_window: int
    public_message_tokens: tuple[int, ...] = ()
    native: bool = True


def _chat_history(messages: list[dict]) -> dict:
    history = []
    for message in messages:
        role = message["role"]
        text = message.get("content") or ""
        if not isinstance(text, str):
            raise ValueError("Multimodal token counting requires image-aware budgeting")
        if role == "tool":
            content = [
                {
                    "type": "toolCallResult",
                    "toolCallId": message.get("tool_call_id"),
                    "content": text,
                }
            ]
        elif role in {"system", "user", "assistant"}:
            reasoning = message.get("reasoning_content") or ""
            if role == "assistant" and reasoning:
                text = f"<think>{reasoning}</think>\n\n{text}"
            content = [{"type": "text", "text": text}]
            for call in message.get("tool_calls") or []:
                function = call["function"]
                arguments = function.get("arguments") or "{}"
                content.append(
                    {
                        "type": "toolCallRequest",
                        "toolCallRequest": {
                            "type": "function",
                            "id": call["id"],
                            "name": function["name"],
                            "arguments": json.loads(arguments)
                            if isinstance(arguments, str)
                            else arguments,
                        },
                    }
                )
        else:
            raise ValueError("Unsupported chat role")
        history.append({"role": role, "content": content})
    return {"messages": history}


async def _measure(host: str, model_id: str, request: dict) -> ContextMeasurement | None:
    # SDK 1.5's websocket uses the transport's 64 KiB receive default. A large
    # prompt-template reply closes that connection before counting can finish.
    # Use the same native, read-only RPC protocol with a bounded frame size;
    # never patch the shared SDK, load a model or run a prediction to count it.
    async with AsyncClient(trust_env=False) as client, aconnect_ws(
        f'ws://{host}/llm', client=client, max_message_size_bytes=8 * 1024 * 1024,
        queue_size=8, keepalive_ping_interval_seconds=None,
    ) as websocket:
        # These ephemeral identifiers have the same resource-lifetime semantics
        # as the SDK's ClientBase authVersion=1 handshake, not a saved app identity.
        await websocket.send_json({'authVersion': 1, 'clientIdentifier': str(uuid4()),
                                   'clientPasskey': str(uuid4())})
        auth = await websocket.receive_json()
        if not isinstance(auth, dict) or auth.get('success') is not True:
            return None
        call_id = 0
        async def rpc(endpoint, parameter=None):
            nonlocal call_id
            if endpoint not in {'listLoaded', 'getModelInfo', 'applyPromptTemplate', 'countTokens'}:
                raise ValueError('token counter only permits read-only operations')
            identifier = call_id
            call_id += 1
            message = {'type': 'rpcCall', 'callId': identifier, 'endpoint': endpoint}
            if parameter is not None:
                message['parameter'] = parameter
            await websocket.send_json(message)
            result = await websocket.receive_json()
            if (not isinstance(result, dict) or result.get('type') != 'rpcResult'
                    or type(result.get('callId')) is not int or result['callId'] != identifier):
                raise ValueError('invalid token counter RPC result')
            return result['result']

        loaded = await rpc('listLoaded')
        if not any(model.get('identifier') == model_id for model in loaded):
            return None
        specifier = {'type': 'query', 'query': {'identifier': model_id}}
        info = await rpc('getModelInfo', {'specifier': specifier, 'throwIfNotFound': True})
        window = info['contextLength']
        if "prompt" in request:
            formatted = request["prompt"]
        else:
            response = await rpc('applyPromptTemplate', {
                'specifier': specifier, 'history': _chat_history(request.get('messages', [])),
                'predictionConfigStack': {'layers': []},
                'opts': {'toolDefinitions': request.get('tools') or []},
            })
            formatted = response['formatted']
        if not isinstance(formatted, str):
            raise ValueError('invalid formatted prompt')
        public_texts = []
        for message in request.get("messages", []):
            text = message.get("content") or ""
            for call in message.get("tool_calls") or []:
                function = call["function"]
                text += "\n" + function["name"] + " " + str(function.get("arguments") or "")
            public_texts.append(text)
        async def count_tokens(text):
            count = (await rpc('countTokens', {'specifier': specifier, 'inputString': text}))['tokenCount']
            if type(count) is not int or count < 0:
                raise ValueError('invalid tokenizer count')
            return count
        count = await count_tokens(formatted)
        public_counts = [await count_tokens(text) + 4 for text in public_texts]
        if type(window) is not int or window <= 0:
            return None
        return ContextMeasurement(count, window, tuple(public_counts))


def measure_context(
    base_url: str, model_id: str, request: dict, *, timeout: float = 2.0
) -> ContextMeasurement | None:
    """Return template/tokenizer measurements, or use the caller's safe fallback.

    Native counter access is limited to local HTTP servers. Other deployments and
    callers already owning an event loop use a bounded read-only worker.
    """
    address = urlparse(base_url)
    if (
        address.scheme != "http"
        or address.hostname not in {"localhost", "127.0.0.1", "::1"}
        or address.username
        or address.password
    ):
        return None
    def count():
        return asyncio.run(asyncio.wait_for(_measure(address.netloc, model_id, request), timeout))

    try:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return count()
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="lmstudio-counter")
        try:
            return executor.submit(count).result(timeout=timeout)
        finally:
            executor.shutdown(wait=False, cancel_futures=True)
    except Exception:
        return None
