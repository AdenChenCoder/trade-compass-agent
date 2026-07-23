from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any

from trade_compass_agent.llm.providers import ChatClient, ChatMessage
from trade_compass_agent.runtime.activity_events import (
    build_tool_end_payload,
    build_tool_start_payload,
)


def run_react_loop(
    *,
    client: ChatClient,
    messages: list[ChatMessage],
    tool_schemas: list[dict[str, Any]],
    execute_tool: Callable[[str, dict[str, Any]], str],
    max_rounds: int = 6,
    on_tool_event: Callable[[str, dict[str, Any]], None] | None = None,
) -> str:
    for _round in range(max_rounds):
        try:
            is_last = (_round == max_rounds - 1)
            completion = client.complete(messages, tools=None if is_last else tool_schemas)
        except Exception as exc:
            return json.dumps(
                {"error": f"LLM call failed: {type(exc).__name__}: {exc}"},
                ensure_ascii=False,
            )
        if completion.tool_calls:
            assistant_tool_calls = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.name, "arguments": tc.arguments},
                }
                for tc in completion.tool_calls
            ]
            messages.append(
                ChatMessage(
                    role="assistant",
                    content=completion.content or "",
                    tool_calls=assistant_tool_calls,
                )
            )
            for tc in completion.tool_calls:
                try:
                    args: dict[str, Any] = {}
                    if tc.arguments and tc.arguments.strip():
                        args = json.loads(tc.arguments)
                except (json.JSONDecodeError, TypeError):
                    args = {}
                if on_tool_event:
                    on_tool_event("tool_start", build_tool_start_payload(tc.name, tc.arguments))
                started_at = time.monotonic()
                try:
                    result = execute_tool(tc.name, args)
                except Exception as exc:
                    result = json.dumps(
                        {"error": f"{type(exc).__name__}: {exc}", "tool": tc.name},
                        ensure_ascii=False,
                    )
                if on_tool_event:
                    on_tool_event("tool_end", build_tool_end_payload(tc.name, started_at, result))
                messages.append(
                    ChatMessage(
                        role="tool",
                        content=result,
                        tool_call_id=tc.id,
                        name=tc.name,
                    )
                )
            continue

        text = (completion.content or "").strip()
        if text:
            return text

    return "已达到嵌套工具轮次上限，请根据已获取数据给出简要结论。"
