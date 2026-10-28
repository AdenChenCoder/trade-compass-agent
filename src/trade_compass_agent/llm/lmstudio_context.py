"""Count local requests with the loaded model's tokenizer without generating."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse


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
    from lmstudio import AsyncClient

    async with AsyncClient(api_host=host) as client:
        # Never load a model or change its load configuration during preflight.
        model = next((m for m in await client.llm.list_loaded() if m.identifier == model_id), None)
        if model is None:
            return None
        if "prompt" in request:
            formatted = request["prompt"]
        else:
            formatted = await model.apply_prompt_template(
                _chat_history(request.get("messages", [])),
                {"toolDefinitions": request.get("tools") or []},
            )
        public_texts = []
        for message in request.get("messages", []):
            text = message.get("content") or ""
            for call in message.get("tool_calls") or []:
                function = call["function"]
                text += "\n" + function["name"] + " " + str(function.get("arguments") or "")
            public_texts.append(text)
        tokenized = await model.tokenize(public_texts) if public_texts else []
        count, window = await model.count_tokens(formatted), await model.get_context_length()
        if count < 0 or window <= 0:
            return None
        return ContextMeasurement(count, window, tuple(len(tokens) + 4 for tokens in tokenized))


def measure_context(
    base_url: str, model_id: str, request: dict, *, timeout: float = 2.0
) -> ContextMeasurement | None:
    """Return template/tokenizer measurements, or use the caller's safe fallback.

    Native SDK access is limited to local HTTP servers. Other deployments and
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
