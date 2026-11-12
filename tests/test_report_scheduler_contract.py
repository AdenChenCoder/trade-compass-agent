"""Exercise task coverage through the scheduled-job consumer boundary."""
import json

import pytest
from fastapi.testclient import TestClient

from report_contract_helpers import report_facts, report_reply
from trade_compass_agent.config import AgentConfig, AppConfig
from trade_compass_agent.llm.providers import ChatCompletion, ToolCall
from trade_compass_agent.ops.agent_session import SCHEDULER_OUTPUT_RULES
from trade_compass_agent.ops.prompt_jobs import PromptJobExecutor, PromptJobStore
from trade_compass_agent.ops.run_store import SqliteRunStore


@pytest.mark.parametrize("missing", [False, True])
@pytest.mark.parametrize("derived", [False, True])
def test_job_coverage_uses_business_task_and_preserves_real_gaps(tmp_path, monkeypatch, missing, derived):
    task = "列出原始收盘价。" + ("计算价格变动并说明方向。" if derived else "") + ("还需比较营收。" if missing else "")
    execution_prompts, publication_tasks = [], []

    class Client:
        name = model = "mock"

        def stream_complete(self, messages, *, tools=None, **kwargs):
            result = report_reply(messages, metrics={"close"})
            if result:
                payload = json.loads(messages[-1].content)
                publication_tasks.append(payload["task"])
                if derived and "schema" in payload:
                    document = json.loads(result.content)
                    refs = [f["id"] for f in report_facts(payload) if f["metric"] == "close"]
                    document["items"].extend([
                        {"id": "direction", "type": "inference", "section": "assessment", "text": "两日收盘价上升。", "refs": ["change"]},
                        {"id": "change", "type": "calculation", "section": "market", "operation": "change_pct", "refs": refs},
                    ])
                    result = ChatCompletion(content=json.dumps(document))
                if "inferences" in payload:
                    # A reviewer must not receive execution/presentation policy
                    # as an additional business requirement, regardless of wording.
                    polluted = SCHEDULER_OUTPUT_RULES in payload["task"]
                    if derived:
                        assert payload["inferences"][0]["calculations"][0]["result"].startswith("13.0303")
                        assert len(payload["inferences"][0]["evidence"]) == 2
                    result = ChatCompletion(content=json.dumps({"reviews": [
                        {"id": "direction", "supported": True, "reason": ""}] if derived else [], "coverage": {
                        "complete": not (polluted or missing),
                        "missing": ["缺少营收依据"] if missing else ["执行提示混入业务任务"] if polluted else [],
                    }}))
                return result
            execution_prompts.extend(m.content for m in messages if m.role == "user")
            if not any(m.role == "tool" for m in messages):
                return ChatCompletion(content="", tool_calls=[ToolCall("bars", "get_bars", '{"symbol":"002415","limit":1}')])
            return ChatCompletion(content="已取得收盘价。")

        complete = stream_complete

    config = AppConfig(data_dir=tmp_path / "data", memory_dir=tmp_path / "memory",
                       data_provider="sample", agent=AgentConfig(learning_enabled=False))
    monkeypatch.setattr("trade_compass_agent.runtime.loop.create_chat_client", lambda _: Client())
    calls = []

    def execute(self, name, arguments):
        calls.append(name)
        assert name == "get_bars"
        return json.dumps({"symbol": "002415", "name": "海康威视", "bars": [
            {"timestamp": "2026-11-09", "close": 6.60}, {"timestamp": "2026-11-10", "close": 7.46}]})

    monkeypatch.setattr("trade_compass_agent.runtime.tools.registry.ToolRegistry.execute", execute)
    monkeypatch.setattr("trade_compass_agent.ops.delivery.DeliveryRouter.deliver", lambda *_: None)
    jobs = PromptJobStore(config.data_dir / "scheduler.db")
    job = jobs.create(name="收盘价", prompt=task, schedule="daily 12:00", delivery_channels=())
    runs = SqliteRunStore(config.data_dir / "scheduler.db")
    PromptJobExecutor(config, runs).execute(job)
    run = runs.recent_runs(job_id=f"custom:{job.id}")[0]
    assert run.status == ("degraded" if missing else "completed")
    assert calls == ["get_bars"]
    assert publication_tasks and all(p.endswith(task) and SCHEDULER_OUTPUT_RULES not in p for p in publication_tasks)
    assert all("本次任务日期：" in p and config.scheduler.timezone in p for p in publication_tasks)
    assert any(SCHEDULER_OUTPUT_RULES in p and task in p for p in execution_prompts)

    monkeypatch.setattr("trade_compass_agent.web.api.load_app_config", lambda: config)
    from trade_compass_agent.web.app import create_app
    detail = TestClient(create_app()).get(f"/api/jobs/runs/{run.id}").json()
    verification = detail["step_runs"][0]["data"]["verification"]
    assert detail["status"] == run.status
    assert verification["execution"] == "completed"
    assert verification["status"] == ("incomplete" if missing else "complete")
    assert "7.46" in detail["analysis"]
    if derived:
        assert "13.03%" in detail["analysis"] and "研判：两日收盘价上升。" in detail["analysis"]
    assert bool(verification["findings"]) is missing
    if missing:
        assert run.error == "缺少营收依据"


def test_repeated_job_reacquires_evidence_and_keeps_complete_history(tmp_path, monkeypatch):
    """A prior tool receipt must not satisfy a later scheduled execution."""
    class Client:
        name = model = "mock"

        def stream_complete(self, messages, **kwargs):
            result = report_reply(messages, metrics={"close"})
            if result:
                return result
            if any(m.role == "tool" for m in messages):
                return ChatCompletion(content="已取得收盘价。")
            return ChatCompletion(content="", tool_calls=[ToolCall(
                "bars", "get_bars", '{"symbol":"002415","limit":1}')])

        complete = stream_complete

    config = AppConfig(data_dir=tmp_path / "data", memory_dir=tmp_path / "memory",
                       data_provider="sample", agent=AgentConfig(learning_enabled=False))
    monkeypatch.setattr("trade_compass_agent.runtime.loop.create_chat_client", lambda _: Client())
    monkeypatch.setattr("trade_compass_agent.ops.delivery.DeliveryRouter.deliver", lambda *_: None)
    calls = []

    def execute(self, name, arguments):
        assert name == "get_bars"
        calls.append(name)
        return json.dumps({"symbol": "002415", "bars": [
            {"timestamp": "2026-11-11", "close": 30 + len(calls)}]})

    monkeypatch.setattr("trade_compass_agent.runtime.tools.registry.ToolRegistry.execute", execute)
    jobs = PromptJobStore(config.data_dir / "scheduler.db")
    job = jobs.create(name="重复查询", prompt="列出本次读取的收盘价。", schedule="sat 23:59", delivery_channels=())
    monkeypatch.setattr("trade_compass_agent.web.api.load_app_config", lambda: config)
    monkeypatch.setattr("trade_compass_agent.web.agent_api.load_app_config", lambda: config)
    from trade_compass_agent.web.app import create_app
    previous = []
    run_ids = []
    session_ids = []
    for index in range(3):
        # Reopen stores/executor/API to cover continuation after restart.
        runs = SqliteRunStore(config.data_dir / "scheduler.db")
        PromptJobExecutor(config, runs).execute(job)
        run = runs.recent_runs(job_id=f"custom:{job.id}")[0]
        api = TestClient(create_app())
        detail = api.get(f"/api/jobs/runs/{run.id}").json()
        assert calls == ["get_bars"] * (index + 1)
        assert detail["status"] == "completed"
        assert str(31 + index) in detail["analysis"]
        sid = detail["step_runs"][0]["data"]["session_id"]
        session_ids.append(sid)
        messages = api.get(f"/api/agent/sessions/{sid}").json()["messages"]
        assert messages[:len(previous)] == previous
        assert len(messages) > len(previous)
        previous = messages
        run_ids.append(run.id)
    assert len(set(session_ids)) == 1
    assert len(set(run_ids)) == 3
    for rid in run_ids:
        assert api.get(f"/api/jobs/runs/{rid}").json()["status"] == "completed"


@pytest.mark.parametrize("scope,requested", [(None, "get_bars"), (set(), "get_bars"),
                                            ({"get_bars"}, "get_bars"), ({"get_bars"}, "place_paper_trade")])
def test_prompt_executor_capabilities_reach_model_and_execution_boundary(tmp_path, monkeypatch, scope, requested):
    observed, executed = [], []

    class Client:
        name = model = "mock"

        def stream_complete(self, messages, *, tools=None, **kwargs):
            reply = report_reply(messages, metrics={"close"})
            if reply:
                return reply
            if not observed:
                observed.append({tool["function"]["name"] for tool in tools or []})
                # Deliberately request an unavailable tool even when the model
                # received an empty schema list. No business write may execute.
                return ChatCompletion(content="", tool_calls=[ToolCall("request", requested, '{"symbol":"002415","limit":1}')])
            return ChatCompletion(content="已结束查询。")

        complete = stream_complete

    config = AppConfig(data_dir=tmp_path/"data", memory_dir=tmp_path/"memory",
                       data_provider="sample", agent=AgentConfig(learning_enabled=False))
    monkeypatch.setattr("trade_compass_agent.runtime.loop.create_chat_client", lambda _: Client())
    monkeypatch.setattr("trade_compass_agent.ops.delivery.DeliveryRouter.deliver", lambda *_: None)

    def execute(self, name, arguments):
        executed.append(name)
        assert name == "get_bars"
        return json.dumps({"symbol": "002415", "bars": [{"timestamp": "2025-01-01", "close": 22}]})

    # Keep the actual registry guard; freeze only the operation behind it.
    monkeypatch.setattr("trade_compass_agent.runtime.tools.registry.ToolRegistry._execute", execute)
    jobs = PromptJobStore(config.data_dir/"scheduler.db")
    job = jobs.create(name="受限查询", prompt="列出收盘价。", schedule="sat 23:59", delivery_channels=())
    runs = SqliteRunStore(config.data_dir/"scheduler.db")
    granted = set(scope) if scope is not None else None
    executor = PromptJobExecutor(config, runs, tool_whitelist=granted)
    if granted is not None:
        granted.add("place_paper_trade")
    executor.execute(job)
    if scope is None:
        assert "get_bars" in observed[0] and "place_paper_trade" in observed[0]
    else:
        assert observed == [scope]
    denied = scope is not None and requested not in scope
    assert executed == ([] if denied else ["get_bars"])
    run = runs.recent_runs(job_id="custom:"+job.id)[0]
    assert run.status == ("degraded" if denied else "completed")
