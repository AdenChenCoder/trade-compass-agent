"""Publication outcomes survive the real scheduler/workflow/API boundaries."""
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from trade_compass_agent.config import AgentConfig, AppConfig, LLMConfig
from trade_compass_agent.llm.providers import ChatCompletion, ToolCall
from trade_compass_agent.ops.run_store import SqliteRunStore
from trade_compass_agent.runtime import loop
from trade_compass_agent.runtime.session import SessionStore
from trade_compass_agent.runtime.tools.registry import ToolRegistry
from trade_compass_agent.runtime.workflows.engine import BUILTIN_WORKFLOW_DIR, load_workflow_asset, run_workflow_asset


@pytest.mark.parametrize("mode", ["complete", "missing_requirement", "review_unavailable", "bad_structure", "source_failed"])
def test_publication_status_survives_workflow_persistence_and_api_reopen(tmp_path, monkeypatch, mode):
    config = AppConfig(data_dir=tmp_path / "data", memory_dir=tmp_path / "memory", data_provider="sample",
                       llm=LLMConfig(provider="disabled"), agent=AgentConfig(learning_enabled=False, llm_session_titles=False))
    original_execute = ToolRegistry.execute
    calls, model_calls, learned = [], [], []
    def execute(self, name, arguments):
        if name.startswith("builtin."):
            return original_execute(self, name, arguments)
        calls.append(name)
        return json.dumps({"symbol": "002415", "error": "timeout"} if mode == "source_failed" else {
            "symbol": "002415", "name": "海康威视", "bars": [{"timestamp": "2026-11-10", "close": 35.17}]})
    def stream(messages, **kwargs):
        from report_contract_helpers import report_reply
        try:
            payload = json.loads(messages[-1].content)
        except ValueError:
            payload = {}
        if "schema" in payload:
            assert kwargs.get("tools") is None
            model_calls.append("compose")
            if mode == "bad_structure":
                return ChatCompletion(content="not a report document")
            return report_reply(messages, gap="行情缺失" if mode == "source_failed" else None)
        if "inferences" in payload:
            assert kwargs.get("tools") is None
            model_calls.append("review")
            if mode == "review_unavailable":
                raise TimeoutError("review offline")
            return ChatCompletion(content=json.dumps({"reviews": [], "coverage": {
                "complete": mode != "missing_requirement", "missing": ["持仓逻辑未评估"] if mode == "missing_requirement" else []}}))
        if messages[-1].role == "tool":
            return ChatCompletion(content="002415当前价格35.17，一旦跌破MA5就重新评估。")
        return ChatCompletion(content=None, tool_calls=[ToolCall("read", "get_bars", '{"symbol":"002415"}')])
    monkeypatch.setattr(loop, "create_chat_client", lambda *_: SimpleNamespace(stream_complete=stream))
    monkeypatch.setattr(loop, "shared_consolidation_worker", lambda **_: None)
    monkeypatch.setattr(loop, "curate_turn_insight", lambda **kw: learned.append(kw))
    monkeypatch.setattr(ToolRegistry, "execute", execute)
    manifest = load_workflow_asset(BUILTIN_WORKFLOW_DIR / "close_check" / "workflow.yaml")
    step = replace(manifest.steps[-1], depends_on=(), with_inputs={"as_of": "{inputs.as_of}", "run_id": "{inputs.run_id}"})
    manifest = replace(manifest, steps=(step,))
    runs = SqliteRunStore(config.data_dir / "scheduler.db")
    run = runs.create_run("close")
    runs.start_run(run)
    result = run_workflow_asset(manifest, {"as_of": "2026-11-10", "run_id": run.id}, config=config)
    data = result["data"]
    verified = data["verification"]
    assert calls == ["get_bars"]
    assert verified["execution"] == "completed"
    assert verified["status"] == ("complete" if mode == "complete" else "incomplete")
    assert bool(result.get("degraded")) is (mode != "complete")
    assert "Agent 执行失败" not in data["analysis"]
    if mode != "complete":
        assert not learned
    if mode == "bad_structure":
        assert model_calls == ["compose"] * 3
    session_id = data["session_id"]
    saved = SessionStore(config.data_dir / "agent_sessions").load(session_id)
    assert saved.messages[-1].verification == verified
    artifact = config.data_dir / "report_artifacts" / f"{verified['artifact_id']}.json"
    assert artifact.exists() and artifact.stat().st_mode & 0o777 == 0o600
    if mode == "complete":
        runs.complete_run(run, message=data["analysis"])
    else:
        runs.degrade_run(run, message=data["analysis"], error=result["error"])
    monkeypatch.setattr("trade_compass_agent.web.api.load_app_config", lambda: config)
    monkeypatch.setattr("trade_compass_agent.web.agent_api.load_app_config", lambda: config)
    monkeypatch.setenv("TRADE_COMPASS_DATA_DIR", str(config.data_dir))
    monkeypatch.setenv("TRADE_COMPASS_MEMORY_DIR", str(config.memory_dir))
    from trade_compass_agent.web.app import create_app
    client = TestClient(create_app(), base_url="http://127.0.0.1")
    messages = client.get(f"/api/agent/sessions/{session_id}").json()["messages"]
    assert messages[-1]["verification"] == verified
    detail = client.get(f"/api/jobs/runs/{run.id}").json()
    public_step = next(item for item in detail["step_runs"] if item["step_id"] == step.id)
    assert public_step["status"] == ("completed" if mode == "complete" else "degraded")
    assert public_step["data"]["verification"] == verified
    assert not (config.data_dir / "paper_trades.jsonl").exists()


def test_custom_job_persists_partial_report_and_api_shows_same_reason(tmp_path, monkeypatch):
    from trade_compass_agent.ops import prompt_jobs
    from trade_compass_agent.runtime.report_contract import Finding, verification_result
    config = AppConfig(data_dir=tmp_path / "data", memory_dir=tmp_path / "memory")
    verified = verification_result([Finding("data", "missing_period", "缺少可比期间")], report="partial", data="insufficient")
    class Session:
        def __init__(self, *args, **kwargs):
            self.verification = verified
            self.degraded = True
            self.session_id = "custom-partial"
        def run(self, prompt):
            return "已有价格已核实，期间比较尚未完成。"
    monkeypatch.setattr(prompt_jobs, "ScheduledAgentSession", Session)
    monkeypatch.setattr("trade_compass_agent.ops.delivery.DeliveryRouter.deliver", lambda *_: None)
    jobs = prompt_jobs.PromptJobStore(config.data_dir / "scheduler.db")
    job = jobs.create(name="比较", prompt="核对期间", schedule="daily 12:00", delivery_channels=())
    runs = SqliteRunStore(config.data_dir / "scheduler.db")
    prompt_jobs.PromptJobExecutor(config, runs).execute(job)
    run = runs.recent_runs(job_id=f"custom:{job.id}")[0]
    assert run.status == "degraded" and run.error == "缺少可比期间"
    monkeypatch.setattr("trade_compass_agent.web.api.load_app_config", lambda: config)
    from trade_compass_agent.web.app import create_app
    data = TestClient(create_app()).get(f"/api/jobs/runs/{run.id}").json()
    assert data["analysis"] == "已有价格已核实，期间比较尚未完成。"
    assert data["step_runs"][0]["status"] == "degraded"
    assert data["step_runs"][0]["data"]["verification"] == verified
