"""Exercise task coverage through the scheduled-job consumer boundary."""
import json

import pytest
from fastapi.testclient import TestClient

from report_contract_helpers import report_reply
from trade_compass_agent.config import AgentConfig, AppConfig
from trade_compass_agent.llm.providers import ChatCompletion, ToolCall
from trade_compass_agent.ops.agent_session import SCHEDULER_OUTPUT_RULES
from trade_compass_agent.ops.prompt_jobs import PromptJobExecutor, PromptJobStore
from trade_compass_agent.ops.run_store import SqliteRunStore


@pytest.mark.parametrize("missing", [False, True])
def test_job_coverage_uses_business_task_and_preserves_real_gaps(tmp_path, monkeypatch, missing):
    task = "列出原始收盘价。" + ("还需比较营收。" if missing else "")
    execution_prompts, publication_tasks = [], []

    class Client:
        name = model = "mock"

        def stream_complete(self, messages, *, tools=None, **kwargs):
            result = report_reply(messages, metrics={"close"})
            if result:
                payload = json.loads(messages[-1].content)
                publication_tasks.append(payload["task"])
                if "inferences" in payload:
                    # A reviewer must not receive execution/presentation policy
                    # as an additional business requirement, regardless of wording.
                    polluted = SCHEDULER_OUTPUT_RULES in payload["task"]
                    result = ChatCompletion(content=json.dumps({"reviews": [], "coverage": {
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
            {"timestamp": "2026-11-10", "close": 7.46}]})

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
    assert bool(verification["findings"]) is missing
    if missing:
        assert run.error == "缺少营收依据"
