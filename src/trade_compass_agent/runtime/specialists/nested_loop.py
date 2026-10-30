from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any

from trade_compass_agent.llm.providers import ChatClient, ChatMessage, check_execution_active
from trade_compass_agent.runtime.activity_events import (
    build_tool_end_payload,
    build_tool_start_payload,
)
from trade_compass_agent.runtime.exceptions import TurnInterruptedError


def run_react_loop(
    *,
    client: ChatClient,
    messages: list[ChatMessage],
    tool_schemas: list[dict[str, Any]],
    execute_tool: Callable[[str, dict[str, Any]], str],
    max_rounds: int = 6,
    on_tool_event: Callable[[str, dict[str, Any]], None] | None = None,
) -> str:
    from trade_compass_agent.llm.native import NativeAgentClient

    tool_started: dict[str, float] = {}

    def finish_tool(tc, result):
        started = tool_started.pop(tc.id, None)
        if started is not None and on_tool_event:
            on_tool_event("tool_end", build_tool_end_payload(tc.name, started, result))

    def execute(tc):
        if on_tool_event:
            on_tool_event("tool_start", build_tool_start_payload(tc.name, tc.arguments))
        tool_started[tc.id] = time.monotonic()
        try:
            args = json.loads(tc.arguments) if tc.arguments and tc.arguments.strip() else {}
            if not isinstance(args, dict):
                raise ValueError("Tool arguments must be a JSON object")
            result = execute_tool(tc.name, args)
        except TurnInterruptedError:
            raise
        except Exception as exc:
            check_execution_active()
            result = json.dumps(
                {"error": f"{type(exc).__name__}: {exc}", "tool": tc.name},
                ensure_ascii=False,
            )
        finish_tool(tc, result)
        return result

    if isinstance(client, NativeAgentClient):
        completion = client.run_agent(messages, tools=tool_schemas, execute_tool=execute,
                                      on_tool_interrupted=finish_tool)
        if completion.incomplete_reason:
            return json.dumps({
                "error": completion.incomplete_reason,
                "incomplete_reason": completion.incomplete_reason,
                "report_markdown": completion.content or "",
                "warnings": [completion.incomplete_reason],
            }, ensure_ascii=False)
        return completion.content or ""

    for _round in range(max_rounds):
        try:
            is_last = (_round == max_rounds - 1)
            completion = client.complete(messages, tools=None if is_last else tool_schemas)
        except TurnInterruptedError:
            raise
        except Exception as exc:
            check_execution_active()
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
                result = execute(tc)
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
