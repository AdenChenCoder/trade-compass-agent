from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace
import time

import pytest

from trade_compass_agent.llm.providers import (
    LLMExecutionCancelled, _request_deadline, check_execution_active, llm_request_deadline,
)
from trade_compass_agent.runtime.specialists.multi_agent.assets import (
    MultiAgentPlanAsset, PlanAgentAsset, PlanStageAsset,
)
from trade_compass_agent.runtime.specialists.multi_agent.strategies import debate_team


def research(monkeypatch, worker):
    agents = {key: PlanAgentAsset(id=key, role=key, prompt="research") for key in ("one", "two")}
    plan = MultiAgentPlanAsset(id="test", version=1, strategy="debate_team", description="",
        stages={"research": PlanStageAsset(id="research", agents=tuple(agents))}, agents=agents)
    events = []
    state = SimpleNamespace(team=SimpleNamespace(id="test"), on_event=events.append)
    monkeypatch.setattr(debate_team, "_run_agent", worker)
    return lambda: debate_team._run_research_stage(state, plan_asset=plan, symbol="600519", context="",
        client=object(), registry=object(), variables={}), events


def test_research_workers_receive_parent_deadline(monkeypatch):
    observed = []
    run, events = research(monkeypatch, lambda **kw: observed.append(_request_deadline.get()) or "研究完成")
    deadline = time.monotonic() + 2
    with llm_request_deadline(deadline):
        assert run() == {"one": "研究完成", "two": "研究完成"}
    assert observed == [deadline, deadline]
    assert sum(e.event == "multi_agent.agent_finished" for e in events) == 2


@pytest.mark.parametrize("cancel", [False, True])
def test_research_workers_stop_at_parent_boundary(monkeypatch, cancel):
    cancelled = Event()
    entered = Event()
    exited = []

    def worker(**kw):
        entered.set()
        try:
            end = time.monotonic() + .8
            while time.monotonic() < end:
                check_execution_active()
                time.sleep(.005)
            return "late result must not be published"
        finally:
            exited.append(kw["agent"].id)

    run, events = research(monkeypatch, worker)
    # The timer only sets a cancellation event; no model/network call is made.
    with ThreadPoolExecutor(max_workers=1) as timer:
        future = timer.submit(lambda: (entered.wait(.5), cancelled.set())) if cancel else None
        started = time.monotonic()
        with llm_request_deadline(started + (.6 if cancel else .06), is_cancelled=cancelled.is_set if cancel else None):
            with pytest.raises(LLMExecutionCancelled if cancel else TimeoutError):
                run()
        elapsed = time.monotonic() - started
        if future:
            future.result()
    assert elapsed < .4
    assert exited
    assert not any(e.event == "multi_agent.agent_finished" for e in events)


def test_late_research_result_does_not_advance_to_debate(monkeypatch):
    cancelled = Event()
    run, events = research(monkeypatch, lambda **kw: cancelled.set() or "late completed text")
    with llm_request_deadline(time.monotonic() + 1, is_cancelled=cancelled.is_set):
        with pytest.raises(LLMExecutionCancelled):
            run()
    assert not any(e.event == "multi_agent.agent_finished" for e in events)
