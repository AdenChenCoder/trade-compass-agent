"""Consumer regressions for the release-blocking review findings."""
import asyncio
import json
import threading
import time
from types import SimpleNamespace

import pytest

from trade_compass_agent.config import AppConfig


def test_connection_switch_updates_next_jobs_and_channel_without_mutating_inflight(client, tmp_path, monkeypatch):
    from trade_compass_agent.config import load_app_config, invalidate_config_cache
    from trade_compass_agent.ops.tick_scheduler import TickScheduler, set_active_scheduler
    from trade_compass_agent.channels import gateway

    path = tmp_path / "config.yaml"
    path.write_text("data_provider: sample\nllm:\n  provider: disabled\n  model: before\n")
    monkeypatch.setenv("TRADE_COMPASS_CONFIG", str(path))
    invalidate_config_cache()
    scheduler = TickScheduler(load_app_config())
    old_executor = scheduler.executor
    monkeypatch.setattr(gateway, "_channel_agent", None)
    monkeypatch.setattr("trade_compass_agent.runtime.loop.AgentLoop.from_config", lambda config: SimpleNamespace(config=config))
    old_agent = gateway._get_channel_agent()
    set_active_scheduler(scheduler)
    try:
        response = client.patch("/api/agent/connection", json={"provider": "lmstudio", "model": "after"})
        assert response.status_code == 200
        assert scheduler.executor.config.llm.model == "after"
        assert scheduler.prompt_executor.config.llm.model == "after"
        assert scheduler.watch_plan_monitor.config.llm.model == "after"
        new_agent = gateway._get_channel_agent()
        assert new_agent.config.llm.model == "after"
        assert gateway._get_channel_agent() is new_agent
        assert old_agent.config.llm.model == old_executor.config.llm.model == "before"
    finally:
        set_active_scheduler(None)
        invalidate_config_cache()


def test_scheduled_timeout_returns_while_late_mutations_remain_rejected(tmp_path, monkeypatch):
    from trade_compass_agent.ops.agent_session import ScheduledAgentSession
    from trade_compass_agent.memory.memory_store import MemoryStore

    release, finished = threading.Event(), threading.Event()
    config = AppConfig(data_dir=tmp_path / "data", memory_dir=tmp_path / "memory")
    memory = MemoryStore(config.memory_dir)
    rejected = []
    agent = SimpleNamespace(_tools=SimpleNamespace(schemas=[]), _memory_store=memory, _skill_store=None)

    def turn(*args, **kwargs):
        release.wait(2)
        try:
            for mutation in (agent._tools.trade_execution_guard, lambda: memory.add("迟到的记忆")):
                try:
                    mutation()
                except Exception as exc:
                    rejected.append(str(exc))
            return SimpleNamespace(summary="late", interrupted=False)
        finally:
            finished.set()

    agent.run_turn = turn
    monkeypatch.setattr("trade_compass_agent.runtime.loop.AgentLoop.from_config", lambda *a, **k: agent)
    start = time.monotonic()
    try:
        with pytest.raises(TimeoutError):
            ScheduledAgentSession(config, job_id="background-review").run("review", timeout=.02)
        assert time.monotonic() - start < .7
        assert not finished.is_set()
    finally:
        release.set()
        assert finished.wait(2)
    assert len(rejected) == 2
    assert not MemoryStore(config.memory_dir).memory_entries


def test_network_timeout_does_not_wait_forever_for_uncooperative_reader():
    from trade_compass_agent.data.network import drain_timeout_workers, run_with_timeout
    release = threading.Event()
    start = time.monotonic()
    try:
        with drain_timeout_workers(), pytest.raises(TimeoutError):
            run_with_timeout(lambda: release.wait(2), .02, "stalled read")
        assert time.monotonic() - start < .7
    finally:
        release.set()


def test_job_timeout_is_observable_before_uncooperative_work_returns(tmp_path, monkeypatch):
    from trade_compass_agent.ops.job_definition import JobDefinition
    from trade_compass_agent.ops.job_executor import JobExecutor
    from trade_compass_agent.ops.run_store import SqliteRunStore
    from trade_compass_agent.runtime.workflows import engine

    release, cancelled = threading.Event(), threading.Event()
    def workflow(*args, cancel_event, **kwargs):
        assert cancel_event.wait(2)
        cancelled.set()
        release.wait(2)
        return {"analysis": "late"}
    monkeypatch.setattr(engine, "run_workflow_asset_by_id", workflow)
    config = AppConfig(data_dir=tmp_path / "data", memory_dir=tmp_path / "memory")
    store = SqliteRunStore(config.data_dir / "scheduler.db")
    job = JobDefinition(id="deadline", name="deadline", description="", schedule="",
                        workflow_id="test", trading_day_only=False, timeout_seconds=.02)
    start = time.monotonic()
    try:
        result = asyncio.run(JobExecutor(config, store).execute(job, trigger="api"))
        assert time.monotonic() - start < .7
        assert cancelled.is_set()
        assert result.status == store.get_run(result.id).status == "timed_out"
    finally:
        release.set()


def test_old_memory_review_cannot_acknowledge_a_changed_version(tmp_path):
    from trade_compass_agent.memory.memory_store import MemoryStore
    store = MemoryStore(tmp_path)
    row = store.add("旧候选", source="agent")
    snapshot = store.background_review_snapshot()
    store.replace("旧候选", "新候选", entry_id=row["entry_id"], expected_version=row["version"])
    assert not store.mark_background_reviewed(snapshot)
    assert MemoryStore(tmp_path).review_due()


def test_workflow_timeout_prevents_late_learning_publication(tmp_path, monkeypatch):
    from dataclasses import replace
    from trade_compass_agent.runtime.workflows import engine
    from trade_compass_agent.llm.providers import LLMExecutionCancelled
    from trade_compass_agent.memory.memory_store import MemoryStore
    from trade_compass_agent.memory.time_tree import TimeTree
    from trade_compass_agent.memory.dream_diary import append_dream_diary
    from trade_compass_agent.memory.patterns import persist_patterns
    from trade_compass_agent.memory.insights import persist_insights

    config = AppConfig(data_dir=tmp_path / "data", memory_dir=tmp_path / "memory", data_provider="sample")
    memory = MemoryStore(config.memory_dir)
    tree = TimeTree(config.data_dir / "time_tree.db")
    release, finished = threading.Event(), threading.Event()
    rejected = []
    mutations = [
        lambda: memory.add("超时产生的内容"),
        lambda: append_dream_diary(config.memory_dir, "超时日记"),
        lambda: persist_patterns(config.memory_dir, []),
        lambda: persist_insights(config.memory_dir, []),
        lambda: tree.seal_day("2026-10-29", [], [], [], [], [], []),
    ]
    def late_step(step, *args, **kwargs):
        try:
            assert release.wait(2)
            for mutation in mutations:
                try:
                    mutation()
                except LLMExecutionCancelled:
                    rejected.append(True)
            return engine.WorkflowStepResult(step.id, step.type, step.uses, {"analysis": "late"})
        finally:
            finished.set()
    original = engine.load_workflow_assets()["close_check"]
    manifest = replace(original, timeout_seconds=.02, steps=(replace(original.steps[-1], depends_on=()),))
    monkeypatch.setattr(engine, "_run_step", late_step)
    try:
        with pytest.raises(TimeoutError):
            engine.run_workflow_asset(manifest, {"as_of": "2026-10-29"}, config=config, run_id="late-learning")
        record = json.loads((config.data_dir / "workflow_runs/late-learning/run.json").read_text())
        assert record["status"] == "timed_out" and not record["artifact_paths"]
    finally:
        release.set()
        assert finished.wait(2)
    assert len(rejected) == len(mutations)
    assert not MemoryStore(config.memory_dir).memory_entries
    assert tree.get_node("2026-10-29") is None
    assert not (config.memory_dir / "DREAM_DIARY.md").exists()
    assert not list(config.memory_dir.glob("dreaming/**/*.json"))


@pytest.mark.parametrize("preflight", [False, True])
def test_cancelled_job_reaches_terminal_state_before_worker_returns(tmp_path, monkeypatch, preflight):
    from trade_compass_agent.ops.job_definition import JobDefinition
    from trade_compass_agent.ops.job_executor import JobExecutor
    from trade_compass_agent.ops.run_store import SqliteRunStore
    from trade_compass_agent.ops.reflection import JobReflection
    from trade_compass_agent.runtime.workflows import engine

    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    def blocked(*args, **kwargs):
        entered.set()
        try:
            release.wait(2)
            return {"analysis": "late"}
        finally:
            finished.set()
    monkeypatch.setattr(engine, "run_workflow_asset_by_id", blocked)
    monkeypatch.setattr(JobReflection, "resolve_pending", blocked)
    config = AppConfig(data_dir=tmp_path / "data", memory_dir=tmp_path / "memory")
    store = SqliteRunStore(config.data_dir / "scheduler.db")
    job = JobDefinition(id="cancel", name="cancel", description="", schedule="", workflow_id="test",
                        agent_session=preflight, trading_day_only=False, timeout_seconds=10)
    async def cancel():
        task = asyncio.create_task(JobExecutor(config, store).execute(job, trigger="api"))
        for _ in range(100):
            if entered.is_set():
                break
            await asyncio.sleep(.01)
        assert entered.is_set()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    start = time.monotonic()
    try:
        asyncio.run(cancel())
        assert time.monotonic() - start < .7
        assert not finished.is_set()
        row = store.recent_runs(job_id="cancel")[0]
        assert row.status == "failed" and "cancelled" in row.error
        assert all(step.status == "failed" for step in store.step_runs_for(row.id))
    finally:
        release.set()
        assert finished.wait(2)


@pytest.mark.parametrize("own_write", [False, True])
def test_background_completion_preserves_concurrent_memory_edits(tmp_path, monkeypatch, own_write):
    from trade_compass_agent.memory.memory_store import MemoryStore
    from trade_compass_agent.runtime.background_review import spawn_background_review
    config = AppConfig(data_dir=tmp_path / "data", memory_dir=tmp_path / "memory")
    memory = MemoryStore(config.memory_dir)
    row = memory.add("待审内容", source="agent")
    snapshot = memory.background_review_snapshot()
    started, release = threading.Event(), threading.Event()
    def review(*args, **kwargs):
        commits = []
        if own_write:
            receipt = MemoryStore(config.memory_dir).record_candidate_review(
                entry_id=row["entry_id"], expected_version=row["version"],
                decision="defer", reason="证据不足", evidence=[])
            commits.append({"tool": "write_knowledge", "result": receipt})
        started.set()
        assert release.wait(2)
        return {"summary": "完成", "commits": commits, "errors": []}
    monkeypatch.setattr("trade_compass_agent.runtime.background_review._run_review_agent_loop", review)
    worker = spawn_background_review([], True, False, None, None, None, config=config,
        source_session_id="review", source_turn_id="one", maintenance_fingerprint=snapshot["fingerprint"])
    try:
        assert started.wait(2)
        current = memory.get_entries_with_meta()[0]
        memory.replace("待审内容", "并发产生的新内容", entry_id=current.entry_id, expected_version=current.version)
    finally:
        release.set()
        worker.join(2)
    assert not worker.is_alive()
    reopened = MemoryStore(config.memory_dir)
    assert reopened.memory_entries == ["并发产生的新内容"]
    assert reopened.review_due()


def test_separate_review_sources_are_persisted_and_drained(tmp_path, monkeypatch):
    from trade_compass_agent.runtime.background_review import spawn_background_review
    release, started = threading.Event(), threading.Event()
    prompts = []
    def review(config, prompt, **kwargs):
        prompts.append(prompt)
        if len(prompts) == 1:
            started.set()
            assert release.wait(2)
        return {"summary": "done", "commits": [], "errors": []}
    monkeypatch.setattr("trade_compass_agent.runtime.background_review._run_review_agent_loop", review)
    config = AppConfig(data_dir=tmp_path / "data", memory_dir=tmp_path / "memory")
    def submit(source):
        return spawn_background_review([{"role": "user", "content": source}], True, False, None, None, None,
                                       config=config, source_session_id=source, source_turn_id="turn")
    first = submit("first-source")
    try:
        assert started.wait(2)
        assert submit("second-source") is None
        assert submit("second-source") is None
        files = list((config.memory_dir / "background_reviews").glob("*.json"))
        assert len(files) == 2
    finally:
        release.set()
        first.join(2)
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and any(json.loads(p.read_text())["status"] != "completed" for p in files):
        time.sleep(.01)
    assert len(prompts) == 2
    assert any("second-source" in prompt for prompt in prompts)
    assert all(json.loads(p.read_text())["status"] == "completed" for p in files)


@pytest.mark.parametrize("text", [
    "2026-10-01 可用现金合计：50000元。",
    "## 2026-10-01 历史记录\n|账户|可用现金|\n|合计|50000|",
    "昨日可用现金合计：50000元；当前可操作资金：10万元。",
])
def test_cash_report_preserves_explicit_history(text):
    from trade_compass_agent.runtime.report_quality import report_issues
    facts = [("analyze_portfolio", json.dumps({"account_balances": [{"id":"test", "available_cash":100000}]}))]
    assert not report_issues(text, facts, reference_date="2026-10-29")


@pytest.mark.parametrize("text", ["当前可操作资金：20万元", "|账户|可用现金（万元）|\n|合计|20|",
                                 "昨日可用现金合计：50000元；当前可操作资金：20万元。"])
def test_cash_report_checks_current_scaled_amounts(text):
    from trade_compass_agent.runtime.report_quality import report_issues
    facts = [("analyze_portfolio", json.dumps({"account_balances": [{"id":"test", "available_cash":100000}]}))]
    assert report_issues(text, facts, reference_date="2026-10-29")
