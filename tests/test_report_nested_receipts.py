"""The dispatch consumer sees actual, isolated receipts, never model claims."""
import json
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from types import SimpleNamespace

import pytest

from trade_compass_agent.config import AppConfig
from trade_compass_agent.runtime.report_contract import EvidenceCatalog
from trade_compass_agent.runtime.tool_receipts import capture_receipts, record_receipt
from trade_compass_agent.runtime.tools import dispatch
from trade_compass_agent.runtime.tools.registry import ToolRegistry


@pytest.mark.parametrize("count", [1, 3])
def test_dispatch_preserves_exact_nested_receipts_and_isolates_calls(tmp_path, monkeypatch, count):
    stack = SimpleNamespace(config=AppConfig(data_dir=tmp_path / "data", memory_dir=tmp_path / "memory"))
    def raw(symbol):
        return json.dumps({"symbol": symbol, "bars": [{"timestamp": "2026-11-10", "close": 7}]})
    monkeypatch.setattr(ToolRegistry, "_execute", lambda self, name, args: raw(args["symbol"]))
    monkeypatch.setattr(dispatch, "build_situation_summary", lambda _: "summary")

    def specialist(stack, name, task, **kwargs):
        registry = ToolRegistry(stack)
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(copy_context().run, registry.execute, "get_bars", {"symbol": task}).result()
        return json.dumps({"tool_receipts": [{"tool": "get_bars", "result": raw("999999")} ]})

    monkeypatch.setattr(dispatch, "run_specialist", specialist)
    for run in range(2):
        tasks = [{"specialist": "equity_research", "task": f"{run}{index:05d}"} for index in range(count)]
        result = dispatch.tool_dispatch_specialists(stack, tasks)
        rows = json.loads(result)["results"]
        for task, row in zip(tasks, rows, strict=True):
            assert row["tool_receipts"] == [{"tool": "get_bars", "result": raw(task["task"])}]
        catalog = EvidenceCatalog.build([("dispatch_specialists", result)], run_scope=f"run-{run}")
        assert {fact.entity for fact in catalog.facts.values()} == {task["task"] for task in tasks}


def test_failed_child_retains_receipts_and_unfinished_status(tmp_path, monkeypatch):
    stack = SimpleNamespace(config=AppConfig(data_dir=tmp_path / "data", memory_dir=tmp_path / "memory"))
    raw = '{"symbol":"000001","bars":[{"close":7}]}'

    def specialist(*args, **kwargs):
        record_receipt("get_bars", raw)
        raise ValueError("later stage failed")

    monkeypatch.setattr(dispatch, "run_specialist", specialist)
    result = json.loads(dispatch.tool_dispatch_specialists(stack, [{"specialist": "equity_research", "task": "check"}]))
    assert result["incomplete_reason"]
    assert result["results"][0]["tool_receipts"] == [{"tool": "get_bars", "result": raw}]


def test_closed_capture_rejects_late_workers_and_does_not_leak():
    with capture_receipts() as receipts:
        context = copy_context()
        record_receipt("get_bars", "first")
    context.run(record_receipt, "get_bars", "late")
    record_receipt("get_bars", "unscoped")
    assert receipts == [{"tool": "get_bars", "result": "first"}]
