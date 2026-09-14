from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import replace
from datetime import date
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from trade_compass_agent.config import AppConfig
from trade_compass_agent.data import providers
from trade_compass_agent.llm.providers import ToolCall
from trade_compass_agent.ops.job_definition import StepContext
from trade_compass_agent.ops.run_store import SqliteRunStore
from trade_compass_agent.runtime import loop
from trade_compass_agent.runtime.tools import batch, builtin_operations
from trade_compass_agent.web import api
from trade_compass_agent.web.app import create_app


def test_baostock_hung_login_is_killed_and_next_request_recovers(tmp_path, monkeypatch):
    sdk = tmp_path / "baostock.py"
    pid_file = tmp_path / "worker.pid"
    sdk.write_text(
        "import os,time\nfrom pathlib import Path\n"
        f"def login():\n Path({str(pid_file)!r}).write_text(str(os.getpid()))\n time.sleep(30)\n"
    )
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))
    provider = providers.BaostockProvider(timeout=0.5)
    started = time.monotonic()
    with pytest.raises(providers.ProviderError, match="[Tt]imeout|timed out"):
        provider.get_bars("600519", limit=1)
    assert time.monotonic() - started < 1.5
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid_file.read_text()), 0)

    sdk.write_text(
        "from types import SimpleNamespace\n"
        "def login(): return SimpleNamespace(error_code='0')\n"
        "class Result:\n"
        " error_code='0'\n i=0\n"
        " def next(self):\n  self.i+=1\n  return self.i==1\n"
        " def get_row_data(self): return ['2026-09-11','10','12','9','11','100','1100','1.5']\n"
        "def query_history_k_data_plus(*args,**kwargs):\n"
        " assert kwargs['adjustflag']=='2'\n return Result()\n"
    )
    bars = providers.BaostockProvider(timeout=2).get_bars("600519", limit=1)
    assert bars[-1].close == 11
    assert bars[-1].amount == 1100


def test_chain_rejects_success_that_arrives_after_budget():
    class LateProvider:
        name = "late"

        def get_bars(self, *args, **kwargs):
            time.sleep(0.25)
            return providers.SampleProvider().get_bars("600519", limit=1)

    started = time.monotonic()
    with pytest.raises(providers.ProviderError, match="timeout"):
        providers.ChainProvider([LateProvider()], total_timeout=0.03).get_bars("600519")
    assert time.monotonic() - started < 0.15


def test_baostock_connection_queue_respects_timeout(monkeypatch):
    slots = threading.BoundedSemaphore(1)
    slots.acquire()
    monkeypatch.setattr(providers, "_BAOSTOCK_SLOTS", slots)
    started = time.monotonic()
    with pytest.raises(providers.ProviderError, match="timed out waiting"):
        providers.BaostockProvider(timeout=0.03).get_bars("600519")
    assert time.monotonic() - started < 0.15
    slots.release()


def test_sina_refusal_backs_off_and_recovers_after_cooldown(monkeypatch):
    import requests

    calls = []
    now = [100.0]
    monkeypatch.setattr(providers.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(providers.SinaDailyProvider, "_cooldown_until", 0.0)
    monkeypatch.setattr(providers.SinaDailyProvider, "_cooldown_status", 0)

    def refused(*args, **kwargs):
        calls.append(1)
        def fail():
            raise requests.HTTPError("456 Client Error")
        return SimpleNamespace(status_code=456, raise_for_status=fail)

    monkeypatch.setattr(requests, "get", refused)
    provider = providers.SinaDailyProvider()
    with pytest.raises(providers.ProviderError, match="456"):
        provider.get_bars("600519")
    with pytest.raises(providers.ProviderError, match="retry after"):
        providers.SinaDailyProvider().get_bars("600201")
    assert len(calls) == 1
    now[0] += 61
    with pytest.raises(providers.ProviderError, match="456"):
        provider.get_bars("600201")
    assert len(calls) == 2


def test_batch_bars_preserves_success_when_one_symbol_hangs(monkeypatch):
    monkeypatch.setattr(batch, "_BATCH_ITEM_TIMEOUT_SECONDS", 0.03, raising=False)

    class Provider:
        def get_bars(self, symbol, **kwargs):
            if symbol == "600519":
                time.sleep(0.25)
            return providers.SampleProvider().get_bars(symbol, limit=2)

    started = time.monotonic()
    result = json.loads(batch.tool_batch_get_bars(SimpleNamespace(provider=Provider()), symbols="600519,600201"))
    assert time.monotonic() - started < 0.15
    assert result["count"] == 1
    assert "600201" in result["results"]
    assert "timed out" in result["errors"]["600519"]


def test_batch_fundamentals_preserves_success_when_one_symbol_hangs(monkeypatch):
    monkeypatch.setattr(batch, "_BATCH_ITEM_TIMEOUT_SECONDS", 0.03)
    monkeypatch.setattr(batch, "_fetch_ulist_batch", lambda _: None)

    class Fundamentals:
        def get_snapshot(self, symbol, **kwargs):
            if symbol == "600519":
                time.sleep(0.25)
            return SimpleNamespace(pe_ttm=10, pb=2, market_cap=100, industry="test", provider_name="test")

    stack = SimpleNamespace(provider=providers.SampleProvider(), fundamentals_provider=Fundamentals())
    started = time.monotonic()
    result = json.loads(batch.tool_batch_get_fundamentals(stack, symbols="600519,600201"))
    assert time.monotonic() - started < 0.15
    assert result["results"]["600201"]["pe_ttm"] == 10
    assert result["results"]["600519"]["error"]


@pytest.mark.parametrize("name", ["batch_get_bars", "batch_get_fundamentals", "batch_search_news"])
def test_batch_tool_has_outer_timeout(name, monkeypatch):
    monkeypatch.setattr(loop, "_BATCH_TOOL_TIMEOUT_SECONDS", 0.03, raising=False)

    class Tools:
        def execute(self, *args):
            time.sleep(0.25)
            return "{}"

    started = time.monotonic()
    results = loop._execute_tool_calls([ToolCall(id="call", name=name, arguments="{}")], Tools(), None)
    assert time.monotonic() - started < 0.15
    assert json.loads(results["call"])["timed_out"]


def test_screening_returns_coverage_and_completed_symbols(tmp_path, monkeypatch):
    import trade_compass_agent.screening.universe as universe
    import trade_compass_agent.screening.engine as engine
    import trade_compass_agent.data.fund_flow as fund_flow

    monkeypatch.setattr(builtin_operations, "_SCREENING_FETCH_TIMEOUT_SECONDS", 0.05, raising=False)
    monkeypatch.setattr(universe, "resolve_universe", lambda _: [SimpleNamespace(symbol=s) for s in ["600519", "600201"]])
    monkeypatch.setattr(universe, "filter_st", lambda s: s)

    class Provider:
        def get_bars(self, symbol, **kwargs):
            if symbol == "600519":
                time.sleep(0.3)
            return providers.SampleProvider().get_bars(symbol, limit=2)

    monkeypatch.setattr(providers, "create_bulk_daily_provider", lambda **kw: Provider())
    monkeypatch.setattr(fund_flow.FundFlowProvider, "get_sector_flow", lambda *a, **kw: [])
    seen = {}

    def screen(df_map, cfg, **kwargs):
        seen.update(df_map)
        return SimpleNamespace(universe_size=len(df_map), l1_passed=1, scored_count=1, top_n=[])

    monkeypatch.setattr(engine, "run_screening", screen)
    ctx = StepContext(config=replace(AppConfig(), data_dir=tmp_path), date=date.today())
    started = time.monotonic()
    output = builtin_operations._run_screening_engine_sync(ctx)
    assert time.monotonic() - started < 0.2
    assert set(seen) == {"600201"}
    assert output.data["coverage"]["requested"] == 2
    assert output.data["coverage"]["received"] == 1
    assert output.data["warnings"]


def test_api_finishes_legacy_running_step_for_timed_out_job(tmp_path, monkeypatch):
    config = replace(AppConfig(), data_dir=tmp_path)
    monkeypatch.setattr(api, "load_app_config", lambda: config)
    store = SqliteRunStore(tmp_path / "scheduler.db")
    run = store.create_run("morning_plan")
    store.start_run(run)
    store.timeout_run(run)
    trace_dir = tmp_path / "workflow_runs" / run.id
    trace_dir.mkdir(parents=True)
    (trace_dir / "trace.jsonl").write_text(json.dumps({
        "event": "builtin.step_started", "data": {"step_id": "screening"},
        "recorded_at": "2026-09-11T01:05:26+00:00",
    }) + "\n")
    response = TestClient(create_app()).get(f"/api/jobs/runs/{run.id}")
    assert response.status_code == 200
    step = next(s for s in response.json()["step_runs"] if s["step_id"] == "screening")
    assert step["status"] == "failed"
    assert step["finished_at"]
    assert step["error"]


def test_workflow_timeout_finishes_trace_and_ignores_late_success(tmp_path, monkeypatch):
    from trade_compass_agent.runtime.workflows import engine

    release = threading.Event()
    original = engine.load_workflow_assets()["close_check"]
    manifest = replace(original, timeout_seconds=1, steps=(replace(original.steps[-1], depends_on=()),))

    def slow_step(step, *args, **kwargs):
        release.wait(3)
        return engine.WorkflowStepResult(step.id, step.type, step.uses, {"analysis": "late report"})

    monkeypatch.setattr(engine, "_run_step", slow_step)
    try:
        output = engine.run_workflow_asset(
            manifest, {"as_of": "2026-09-11"},
            config=AppConfig(data_dir=tmp_path, data_provider="sample"), run_id="timeout-test",
        )
        assert output["degraded"]
        path = tmp_path / "workflow_runs/timeout-test/trace.jsonl"
        before = path.read_text()
        events = [json.loads(line) for line in before.splitlines()]
        finished = [e for e in events if e["event"] == "builtin.step_finished"]
        assert len(finished) == 1
        assert finished[0]["data"]["status"] == "failed"
    finally:
        release.set()
    time.sleep(0.05)
    assert path.read_text() == before


def test_partial_screening_warnings_reach_workflow_artifact(tmp_path, monkeypatch):
    from trade_compass_agent.runtime.workflows import engine

    class Tools:
        def __init__(self, stack):
            pass

        def execute(self, name, args):
            if name == "builtin.run_screening_engine":
                return json.dumps({"data": {"coverage": {"received": 1, "requested": 2}, "warnings": ["行情覆盖1/2"]}})
            return json.dumps({"data": {"analysis": "有数据缺口的分析"}})

    original = engine.load_workflow_assets()["morning_plan"]
    manifest = replace(original, steps=(original.steps[0], replace(original.steps[-1], depends_on=("screening",))))
    monkeypatch.setattr(engine, "ToolRegistry", Tools)
    output = engine.run_workflow_asset(manifest, {"as_of": "2026-09-11"},
        config=AppConfig(data_dir=tmp_path, data_provider="sample"), run_id="partial")
    assert "行情覆盖1/2" in output["warnings"]


def test_primary_failure_is_degraded_in_persisted_workflow_record(tmp_path, monkeypatch):
    from trade_compass_agent.runtime.workflows import engine

    original = engine.load_workflow_assets()["close_check"]
    manifest = replace(original, steps=(replace(original.steps[-1], depends_on=()),))
    monkeypatch.setattr(engine, "_run_step", lambda step, *a, **kw: engine.WorkflowStepResult(
        step.id, step.type, step.uses, {"error": "data unavailable"},
    ))
    output = engine.run_workflow_asset(manifest, {"as_of": "2026-09-11"},
        config=AppConfig(data_dir=tmp_path, data_provider="sample"), run_id="failed-primary")
    record = json.loads((tmp_path / "workflow_runs/failed-primary/run.json").read_text())
    assert output["degraded"]
    assert record["status"] == "degraded"
    assert "data unavailable" in record["error"]


def test_non_autonomous_scheduled_agent_stops_late_mutations(tmp_path, monkeypatch):
    from trade_compass_agent.ops.agent_session import ScheduledAgentSession
    from trade_compass_agent.portfolio.trading_policy import TradeRejected

    done = threading.Event()
    seen = {}
    agent = SimpleNamespace(_tools=SimpleNamespace(schemas=[]))

    def run_turn(prompt, **kwargs):
        time.sleep(0.12)
        seen["cancelled"] = kwargs["is_cancelled"]()
        try:
            agent._tools.trade_execution_guard()
        except TradeRejected:
            seen["rejected"] = True
        done.set()
        return SimpleNamespace(summary="late report")

    agent.run_turn = run_turn
    monkeypatch.setattr(loop.AgentLoop, "from_config", lambda *a, **kw: agent)
    session = ScheduledAgentSession(AppConfig(data_dir=tmp_path), job_id="close")
    with pytest.raises(TimeoutError):
        session.run("test", timeout=0.03)
    assert done.wait(1)
    assert seen == {"cancelled": True, "rejected": True}
