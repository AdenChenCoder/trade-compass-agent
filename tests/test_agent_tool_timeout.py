from __future__ import annotations

import json
import time

from trade_compass_agent.llm.providers import ToolCall
from trade_compass_agent.runtime import loop


class _SlowTools:
    def execute(self, name: str, arguments: str) -> str:
        time.sleep(0.2)
        return json.dumps({"name": name})


def test_parallel_tool_calls_return_timeout_results(monkeypatch):
    monkeypatch.setattr(loop, "_PARALLEL_TOOL_TIMEOUT_SECONDS", 0.01)
    calls = [
        ToolCall(id="first", name="eastmoney_news", arguments="{}"),
        ToolCall(id="second", name="search_market_flash", arguments="{}"),
    ]

    started = time.monotonic()
    results = loop._execute_tool_calls(calls, _SlowTools(), is_cancelled=None)

    assert time.monotonic() - started < 0.15
    assert json.loads(results["first"])["timed_out"] is True
    assert json.loads(results["second"])["timed_out"] is True
