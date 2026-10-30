from __future__ import annotations

import json
import threading
import time
from types import SimpleNamespace

import pytest

from trade_compass_agent.config import AppConfig
from trade_compass_agent.llm.providers import (
    LLMExecutionCancelled, _remaining_request_time, check_execution_active, llm_request_deadline,
)
from trade_compass_agent.runtime.exceptions import TurnInterruptedError
from trade_compass_agent.runtime.tools import dispatch


@pytest.fixture
def stack(tmp_path):
    return SimpleNamespace(config=AppConfig(data_dir=tmp_path / "data", memory_dir=tmp_path / "memory"))


def tasks(count):
    return [{"specialist": f"expert-{index}", "task": "分析行情"} for index in range(count)]


@pytest.mark.parametrize("count", [1, 2])
@pytest.mark.parametrize("exception_type", [TurnInterruptedError, LLMExecutionCancelled])
def test_dispatch_propagates_terminal_signal(stack, monkeypatch, count, exception_type):
    error = exception_type("stop")
    events = []
    summaries = []

    def run(*args, **kwargs):
        raise error

    monkeypatch.setattr(dispatch, "run_specialist", run)
    monkeypatch.setattr(dispatch, "build_situation_summary", lambda stack: summaries.append(True))
    with pytest.raises(exception_type) as caught:
        dispatch.tool_dispatch_specialists(stack, tasks(count), on_event=events.append)
    assert caught.value is error
    assert not summaries
    assert all(event.event == "specialist_started" for event in events)


@pytest.mark.parametrize("count", [1, 2])
@pytest.mark.parametrize("exception_type", [ValueError, TimeoutError])
def test_dispatch_retains_recoverable_specialist_errors(stack, monkeypatch, count, exception_type):
    def run(*args, **kwargs):
        raise exception_type("行情服务暂时不可用")

    monkeypatch.setattr(dispatch, "run_specialist", run)
    monkeypatch.setattr(dispatch, "build_situation_summary", lambda stack: {"summary": "行情暂时不可用"})
    events = []
    with llm_request_deadline(time.monotonic() + 30):
        result = json.loads(dispatch.tool_dispatch_specialists(stack, tasks(count), on_event=events.append))
    assert len(result["results"]) == count
    assert all(exception_type.__name__ in json.loads(item["output"])["error"] for item in result["results"])
    assert [event.data["status"] for event in events if event.event == "specialist_done"] == ["error"] * count


@pytest.mark.parametrize("count", [1, 2])
@pytest.mark.parametrize("termination", ["cancel", "deadline"])
def test_dispatch_workers_share_parent_budget_and_finish_before_return(stack, monkeypatch, count, termination):
    barrier = threading.Barrier(count)
    cancelled = threading.Event()
    finished = []
    normal_returns = []
    inherited_budgets = []
    events = []

    def run(stack, name, task, **kwargs):
        try:
            inherited_budgets.append(_remaining_request_time())
            barrier.wait(timeout=1)
            if termination == "cancel":
                if name == "expert-0":
                    cancelled.set()
                assert cancelled.wait(timeout=1)
            while _remaining_request_time() is not None:
                check_execution_active()
                threading.Event().wait(0.005)
            normal_returns.append(name)
            return "父执行预算未继承"
        finally:
            finished.append(name)

    monkeypatch.setattr(dispatch, "run_specialist", run)
    monkeypatch.setattr(dispatch, "build_situation_summary", lambda stack: pytest.fail("Cancelled work must not be summarized"))
    deadline = time.monotonic() + (5 if termination == "cancel" else 0.1)
    error = LLMExecutionCancelled if termination == "cancel" else TimeoutError
    started = time.monotonic()
    with llm_request_deadline(deadline, is_cancelled=cancelled.is_set if termination == "cancel" else None):
        with pytest.raises(error):
            dispatch.tool_dispatch_specialists(stack, tasks(count), on_event=events.append)

    assert time.monotonic() - started < 2
    assert sorted(finished) == [f"expert-{index}" for index in range(count)]
    assert all(value is not None for value in inherited_budgets)
    assert not normal_returns
    assert all(event.event == "specialist_started" for event in events)
    assert _remaining_request_time() is None


@pytest.mark.parametrize("exception_type", [TurnInterruptedError, LLMExecutionCancelled])
def test_dispatch_summary_does_not_swallow_terminal_signal(stack, monkeypatch, exception_type):
    error = exception_type("stop")
    monkeypatch.setattr(dispatch, "run_specialist", lambda *args, **kwargs: "研究报告")

    def summarize(stack):
        raise error

    monkeypatch.setattr(dispatch, "build_situation_summary", summarize)
    with pytest.raises(exception_type) as caught:
        dispatch.tool_dispatch_specialists(stack, tasks(2))
    assert caught.value is error
