import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from trade_compass_agent.config import AppConfig
from trade_compass_agent.ops.run_store import SqliteRunStore
from trade_compass_agent.runtime.background_review import (
    is_committed_mutation, spawn_background_review, _run_review_agent_loop,
)


def test_only_successful_mutations_count():
    assert not is_committed_mutation("skill_manage", {"action": "view"}, {"ok": True, "changed": True})
    assert not is_committed_mutation("skill_manage", {"action": "patch"}, {"ok": False})
    assert not is_committed_mutation("write_knowledge", {"action": "add"}, {"ok": True, "changed": False})
    assert is_committed_mutation("skill_manage", {"action": "patch"}, {"ok": True, "changed": True})


def test_source_evidence_unique_session_and_idempotent_review(tmp_path, monkeypatch):
    from trade_compass_agent.ops.agent_session import ScheduledAgentSession
    seen = []
    def run(self, prompt, timeout):
        seen.append((self.session_id, self._memory_actor, prompt))
        requests = [json.loads(path.read_text()) for path in
                    (self.config.memory_dir / "background_reviews").glob("*.json")]
        assert any(self.session_id in item["attempt_sessions"].values() for item in requests)
        return "Nothing to save."
    monkeypatch.setattr(ScheduledAgentSession, "run", run)
    config = AppConfig(data_dir=tmp_path / "data", memory_dir=tmp_path / "vault")
    messages = [{"role": "user", "content": "只检查原任务"}, {"role": "tool", "name": "paper_trade", "content": '{"ok":false,"error":"stale_quote"}'}]
    def spawn(turn):
        return spawn_background_review(messages, True, True, None, None, None, config=config, source_session_id="origin", source_turn_id=turn)
    t = spawn("turn1")
    t.join(timeout=10)
    assert not t.is_alive()
    assert spawn("turn1") is None
    t2 = spawn("turn2")
    t2.join(timeout=10)
    assert len(seen) == 2 and seen[0][0] != seen[1][0]
    assert all(actor == "background_review" and "stale_quote" in prompt and "origin" in prompt for _, actor, prompt in seen)
    runs = SqliteRunStore(config.data_dir / "scheduler.db").recent_runs()
    assert all(r.status == "completed" and r.artifact for r in runs)
    assert all(Path(r.artifact).is_file() for r in runs)


def test_failed_review_retains_inputs_and_does_not_reset_nudges(tmp_path, monkeypatch):
    monkeypatch.setattr("trade_compass_agent.runtime.background_review._run_review_agent_loop", lambda *a, **k: (_ for _ in ()).throw(TimeoutError("stopped")))
    config = AppConfig(data_dir=tmp_path / "data", memory_dir=tmp_path / "vault")
    callbacks = []
    t = spawn_background_review([{"role": "user", "content": "保留此证据"}], True, False, None, None, None,
        config=config, source_session_id="origin", source_turn_id="failed", on_complete=lambda *a: callbacks.append(a))
    t.join(timeout=10)
    assert not callbacks
    request = json.loads(next((config.memory_dir / "background_reviews").glob("*.json")).read_text())
    assert request["messages"][0]["content"] == "保留此证据" and request["status"] == "retry_pending"
    assert SqliteRunStore(config.data_dir / "scheduler.db").recent_runs()[0].status == "timed_out"


def test_receipts_report_partial_failure_and_ignore_read_only_calls(tmp_path, monkeypatch):
    def run(self, prompt, timeout):
        path = self.config.data_dir / "agent_sessions" / f"{self.session_id}.jsonl"
        path.parent.mkdir(parents=True)
        records = []
        for id, action, result in [("1", "view", {"ok": True}), ("2", "patch", {"ok": True, "changed": True, "version": "v2"}), ("3", "patch", {"ok": False, "error": "bad anchor"})]:
            records += [{"role": "assistant", "tool_calls": [{"id": id, "function": {"name": "skill_manage", "arguments": json.dumps({"action": action, "name": "one" if id == "2" else "two"})}}]},
                        {"role": "tool", "tool_call_id": id, "content": json.dumps(result)}]
        path.write_text("\n".join(json.dumps(r) for r in records))
        return "全部完成"  # prose cannot override failure receipts
    monkeypatch.setattr("trade_compass_agent.ops.agent_session.ScheduledAgentSession.run", run)
    config = AppConfig(data_dir=tmp_path / "data", memory_dir=tmp_path / "vault")
    result = _run_review_agent_loop(config, "test", request_id="test")
    assert len(result["commits"]) == 1 and result["errors"] == ["bad anchor"]


def test_scheduled_interruption_is_not_replaced_by_an_older_report(tmp_path, monkeypatch):
    from trade_compass_agent.ops.agent_session import ScheduledAgentSession
    from trade_compass_agent.runtime.exceptions import AgentUnavailableError
    fake = SimpleNamespace(_tools=SimpleNamespace(schemas=[]), _memory_store=SimpleNamespace(), _skill_store=SimpleNamespace(),
                           run_turn=lambda *a, **k: SimpleNamespace(summary="已停止", interrupted=True))
    monkeypatch.setattr("trade_compass_agent.runtime.loop.AgentLoop.from_config", lambda *a, **k: fake)
    config = AppConfig(data_dir=tmp_path / "data", memory_dir=tmp_path / "vault")
    with pytest.raises(AgentUnavailableError, match="interrupted"):
        ScheduledAgentSession(config, job_id="background-review").run("test", timeout=2)


@pytest.mark.parametrize("fresh", [False, True])
def test_last_review_attempt_recovers_to_visible_terminal_state(tmp_path, monkeypatch, fresh):
    from trade_compass_agent.runtime.background_review import resume_background_reviews
    monkeypatch.setattr("trade_compass_agent.runtime.background_review.time.time", lambda: 1000)
    config = AppConfig(data_dir=tmp_path / "data", memory_dir=tmp_path / "vault")
    runs = SqliteRunStore(config.data_dir / "scheduler.db")
    run = runs.create_run("background-review", trigger="agent")
    runs.start_run(run)
    step = runs.create_step_run(run.id, "review-3")
    runs.start_step(step)
    root = config.memory_dir / "background_reviews"
    root.mkdir(parents=True)
    path = root / "last-attempt.json"
    partial = {"commits": [{"tool": "skill_manage", "result": {"version": "saved-version"}}]}
    request = {"id": "last-attempt", "run_id": run.id, "status": "running", "attempts": 3,
               "started_epoch": 999 if fresh else 0, "messages": [{"role": "user", "content": "来源证据"}], "result": partial}
    path.write_text(json.dumps(request))
    resume_background_reviews(config)
    recovered = json.loads(path.read_text())
    assert recovered["status"] == ("running" if fresh else "needs_attention")
    assert recovered["attempts"] == 3 and recovered["result"] == partial
    assert recovered["messages"] == request["messages"]
    assert runs.get_run(run.id).status == ("running" if fresh else "failed")
    assert runs.step_runs_for(run.id)[0].status == ("running" if fresh else "failed")
    if not fresh:
        assert "需要处理" in runs.get_run(run.id).message
        before = path.read_bytes()
        resume_background_reviews(config)
        assert path.read_bytes() == before


def test_recovery_preserves_completion_committed_before_process_exit(tmp_path, monkeypatch):
    from trade_compass_agent.runtime.background_review import resume_background_reviews
    monkeypatch.setattr("trade_compass_agent.runtime.background_review.time.time", lambda: 1000)
    config = AppConfig(data_dir=tmp_path / "data", memory_dir=tmp_path / "vault")
    runs = SqliteRunStore(config.data_dir / "scheduler.db")
    run = runs.create_run("background-review", trigger="agent")
    runs.complete_run(run, message="复盘完成")
    root = config.memory_dir / "background_reviews"
    root.mkdir(parents=True)
    path = root / "last-attempt.json"
    path.write_text(json.dumps({"run_id": run.id, "status": "running", "attempts": 3, "started_epoch": 0}))
    resume_background_reviews(config)
    assert json.loads(path.read_text())["status"] == "completed"
    assert runs.get_run(run.id).status == "completed"


@pytest.mark.parametrize("attempt", [1, 2, 3])
@pytest.mark.parametrize("fresh", [False, True])
@pytest.mark.parametrize("previous_result", [False, True])
def test_every_completed_attempt_recovers_receipts_without_reexecution(
    tmp_path, monkeypatch, client, attempt, fresh, previous_result,
):
    from trade_compass_agent.runtime.background_review import resume_background_reviews

    monkeypatch.setattr("trade_compass_agent.runtime.background_review.time.time", lambda: 1000)
    calls = []
    monkeypatch.setattr("trade_compass_agent.runtime.background_review.threading.Thread",
                        lambda *a, **k: calls.append(k))
    config = AppConfig(data_dir=tmp_path / "data", memory_dir=tmp_path / "vault")
    runs = SqliteRunStore(config.data_dir / "scheduler.db")
    run = runs.create_run("background-review", trigger="agent")
    runs.start_run(run)
    source = {"request_id": "review", "source_session_id": "origin", "source_turn_id": "turn"}
    result = {"session_id": f"review-{attempt}", "summary": "已更新 Skill",
              "commits": [{"tool": "skill_manage", "result": {"version": "saved-v2"}}], "errors": []}
    if attempt > 1:
        previous = runs.create_step_run(run.id, f"review-{attempt - 1}")
        runs.complete_step(previous, data_json=json.dumps({**source, "summary": "上次失败", "errors": ["timeout"]}))
        runs.fail_step(previous, "timeout")
    step = runs.create_step_run(run.id, f"review-{attempt}")
    runs.start_step(step)
    runs.complete_step(step, output=result["summary"], data_json=json.dumps({**source, **result}))
    root = config.memory_dir / "background_reviews"
    root.mkdir(parents=True)
    path = root / "review.json"
    runs.complete_run(run, message="复盘完成，提交 1 项修改", artifact=str(path))
    before_run, before_steps = runs.get_run(run.id), runs.step_runs_for(run.id)
    request = {"id": "review", "run_id": run.id, "status": "running", "attempts": attempt,
               "started_epoch": 999 if fresh else 0, "messages": [{"role": "user", "content": "来源证据"}],
               "review_memory": True, "review_skills": True, "source_session_id": "origin", "source_turn_id": "turn",
               "error": "上次失败", "retry_after": 2000}
    if previous_result:
        request["result"] = {"summary": "上次失败", "commits": [], "errors": ["timeout"]}
    path.write_text(json.dumps(request))

    resume_background_reviews(config)

    recovered = json.loads(path.read_text())
    assert not calls  # No worker, model call, or repeated mutation.
    assert recovered["status"] == "completed" and recovered["attempts"] == attempt
    assert recovered["result"] == result
    assert recovered["messages"] == request["messages"]
    assert recovered["finished_at"] == before_run.finished_at.isoformat()
    assert "error" not in recovered and "retry_after" not in recovered
    assert runs.get_run(run.id) == before_run and runs.step_runs_for(run.id) == before_steps
    before_request = path.read_bytes()
    resume_background_reviews(config)
    assert path.read_bytes() == before_request

    monkeypatch.setattr("trade_compass_agent.web.api.load_app_config", lambda: config)
    response = client.get(f"/api/jobs/runs/{run.id}")
    assert response.status_code == 200
    visible = response.json()
    assert visible["status"] == "completed" and visible["message"] == before_run.message
    assert visible["step_runs"][-1]["data"]["commits"] == result["commits"]


@pytest.mark.parametrize("attempt", [1, 2])
def test_unfinished_review_still_retries_with_previous_commits(tmp_path, monkeypatch, attempt):
    from trade_compass_agent.runtime.background_review import _dispatch_request

    config = AppConfig(data_dir=tmp_path / "data", memory_dir=tmp_path / "vault")
    runs = SqliteRunStore(config.data_dir / "scheduler.db")
    run = runs.create_run("background-review", trigger="agent")
    runs.degrade_run(run, error="timeout", message="复盘存在未完成修改")
    root = config.memory_dir / "background_reviews"
    root.mkdir(parents=True)
    path = root / "retry.json"
    partial = {"summary": "部分完成", "commits": [{"tool": "skill_manage", "result": {"version": "saved-v1"}}],
               "errors": ["timeout"]}
    path.write_text(json.dumps({"id": "retry", "run_id": run.id, "status": "retry_pending", "attempts": attempt,
        "source_session_id": "origin", "source_turn_id": "turn", "messages": [{"role": "user", "content": "来源证据"}],
        "review_memory": True, "review_skills": True, "retry_after": 0, "result": partial}))
    calls, callbacks = [], []
    def review(config, prompt, **kwargs):
        calls.append((prompt, kwargs["attempt"]))
        return {"summary": "剩余修改已完成", "commits": [], "errors": []}
    monkeypatch.setattr("trade_compass_agent.runtime.background_review._run_review_agent_loop", review)
    thread = _dispatch_request(config, path, on_complete=lambda *args: callbacks.append(args))
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert len(calls) == 1 and calls[0][1] == attempt + 1
    assert "saved-v1" in calls[0][0] and "来源证据" in calls[0][0]
    assert callbacks == [(True, True)]
    assert runs.get_run(run.id).status == "completed"
    recovered = json.loads(path.read_text())
    assert recovered["status"] == "completed" and recovered["attempts"] == attempt + 1
    assert recovered["result"]["commits"] == partial["commits"]
    assert "提交 1 项" in runs.get_run(run.id).message
    assert _dispatch_request(config, path) is None


@pytest.mark.parametrize("attempt", [1, 2, 3])
@pytest.mark.parametrize("stored_session", [False, True])
def test_retry_after_mid_review_crash_recovers_already_committed_receipts(
    tmp_path, monkeypatch, client, attempt, stored_session,
):
    from trade_compass_agent.ops.agent_session import ScheduledAgentSession
    from trade_compass_agent.runtime.background_review import _dispatch_request
    from trade_compass_agent.memory.skill_store import SkillStore
    config = AppConfig(data_dir=tmp_path / "data", memory_dir=tmp_path / "vault")
    skills = SkillStore(config.memory_dir / "skills")
    content = "---\nname: review\ndescription: Test reusable process\ncategory: analysis\n---\n\n只在行情有效时核查条件。\n"
    skills.create("review", content)
    receipt = skills.patch("review", "核查条件", "核查条件和例外")
    assert receipt["ok"]
    runs = SqliteRunStore(config.data_dir / "scheduler.db")
    run = runs.create_run("background-review", trigger="agent")
    runs.start_run(run)
    step = runs.create_step_run(run.id, f"review-{attempt}")
    runs.start_step(step)
    # Startup reaps the interrupted run, but the tool's durable receipt exists.
    runs.fail_step(step, "process interrupted")
    runs.fail_run(run, error="process interrupted", message="interrupted")
    session = ScheduledAgentSession(config, job_id="background-review", step_id=f"crashed-{attempt}")
    if stored_session:
        session.session_id = "saved-review-session-before-restart"
    trace = config.data_dir / "agent_sessions" / f"{session.session_id}.jsonl"
    trace.parent.mkdir(parents=True)
    rows = [{"role": "assistant", "tool_calls": [{"id": "patch-1", "function": {"name": "skill_manage",
        "arguments": json.dumps({"action": "patch", "name": "review"})}}]},
        {"role": "tool", "tool_call_id": "patch-1", "content": json.dumps(receipt)}]
    trace.write_text("\n".join(json.dumps(row) for row in rows))
    root = config.memory_dir / "background_reviews"
    root.mkdir()
    path = root / "crashed.json"
    committed = {"tool": "skill_manage", "action": "patch", "result": receipt}
    path.write_text(json.dumps({"id": "crashed", "run_id": run.id, "status": "running", "attempts": attempt,
        "started_epoch": 0, "source_session_id": "origin", "source_turn_id": "turn",
        "messages": [{"role": "user", "content": "review current conditions"}], "review_memory": True, "review_skills": True,
        "attempt_sessions": {str(attempt): session.session_id} if stored_session else {},
        "result": {"commits": [committed]} if stored_session else {}}))
    prompts = []
    def review(config, prompt, **kwargs):
        prompts.append(prompt)
        return {"summary": "Nothing to save.", "commits": [], "errors": []}
    monkeypatch.setattr("trade_compass_agent.runtime.background_review._run_review_agent_loop", review)
    worker = _dispatch_request(config, path)
    if attempt < 3:
        worker.join(timeout=5)
        assert not worker.is_alive() and len(prompts) == 1
        assert receipt["version"] in prompts[0]
    else:
        assert worker is None and not prompts
    recovered = json.loads(path.read_text())
    committed["receipt_id"] = f"{session.session_id}:patch-1:0"
    assert recovered["result"]["commits"] == [committed]
    assert recovered["status"] == ("completed" if attempt < 3 else "needs_attention")
    assert skills.view("review")["version"] == receipt["version"]
    monkeypatch.setattr("trade_compass_agent.web.api.load_app_config", lambda: config)
    response = client.get(f"/api/jobs/runs/{run.id}")
    assert response.status_code == 200
    assert response.json()["step_runs"][-1]["data"]["commits"] == [committed]
    before = path.read_bytes()
    assert _dispatch_request(config, path) is None and path.read_bytes() == before


def test_interrupted_trace_keeps_complete_receipts_and_marks_unknown_write(tmp_path):
    from trade_compass_agent.runtime.background_review import _read_review_receipts
    config = AppConfig(data_dir=tmp_path, memory_dir=tmp_path / "vault")
    trace = tmp_path / "agent_sessions" / "interrupted.jsonl"
    trace.parent.mkdir()
    trace.write_text("\n".join(json.dumps(row) for row in [
        {"role": "assistant", "tool_calls": [{"id": "done", "function": {
            "name": "skill_manage", "arguments": '{"action":"patch","name":"one"}'}}]},
        {"role": "tool", "tool_call_id": "done", "content": '{"ok":true,"changed":true,"version":"v2"}'},
        {"role": "assistant", "tool_calls": [{"id": "unknown", "function": {
            "name": "skill_manage", "arguments": '{"action":"patch","name":"two"}'}}]},
    ]) + '\n{"role":"tool",')
    result = _read_review_receipts(config, "interrupted", "")
    assert len(result["commits"]) == 1 and result["commits"][0]["result"]["version"] == "v2"
    assert any("缺少回执" in error for error in result["errors"])
    assert any("记录第 4 行不完整" in error for error in result["errors"])


def test_recovery_does_not_attribute_later_commits_to_an_earlier_attempt(tmp_path, monkeypatch, client):
    from trade_compass_agent.runtime.background_review import _dispatch_request
    config = AppConfig(data_dir=tmp_path / "data", memory_dir=tmp_path / "vault")
    runs = SqliteRunStore(config.data_dir / "scheduler.db")
    run = runs.create_run("background-review", trigger="agent")
    runs.fail_run(run, error="interrupted")
    root = config.memory_dir / "background_reviews"
    root.mkdir(parents=True)
    traces = config.data_dir / "agent_sessions"
    traces.mkdir()
    commits, sessions = [], {}
    for attempt in (1, 2):
        step = runs.create_step_run(run.id, f"review-{attempt}")
        runs.start_step(step)
        runs.complete_step(step, data_json=json.dumps({"summary": f"attempt {attempt}"}))
        runs.fail_step(step, "interrupted")
        session_id = f"scheduler-background-review-history-{attempt}-2026-09-13"
        sessions[str(attempt)] = session_id
        receipt = {"ok": True, "changed": True, "version": f"v{attempt}"}
        commits.append({"receipt_id": f"{session_id}:{attempt}:0",
                        "tool": "skill_manage", "action": "patch", "result": receipt})
        (traces / f"{session_id}.jsonl").write_text("\n".join(json.dumps(row) for row in [
            {"role": "assistant", "tool_calls": [{"id": str(attempt), "function": {
                "name": "skill_manage", "arguments": '{"action":"patch","name":"review"}'}}]},
            {"role": "tool", "tool_call_id": str(attempt), "content": json.dumps(receipt)},
        ]))
    path = root / "history.json"
    path.write_text(json.dumps({"id": "history", "run_id": run.id, "status": "running", "attempts": 2,
        "started_epoch": 0, "attempt_sessions": sessions, "source_session_id": "origin", "source_turn_id": "turn",
        "messages": [], "review_memory": True, "review_skills": True,
        "result": {"commits": commits, "summary": "previous attempts", "errors": []}}))
    monkeypatch.setattr("trade_compass_agent.runtime.background_review._run_review_agent_loop",
        lambda *args, **kwargs: {"summary": "Nothing to save.", "commits": [], "errors": []})
    worker = _dispatch_request(config, path)
    worker.join(timeout=5)
    assert not worker.is_alive()
    monkeypatch.setattr("trade_compass_agent.web.api.load_app_config", lambda: config)
    response = client.get(f"/api/jobs/runs/{run.id}")
    assert response.status_code == 200
    steps = {step["step_id"]: step["data"]["commits"] for step in response.json()["step_runs"]}
    assert steps == {"review-1": commits[:1], "review-2": commits[1:], "review-3": commits}
    assert [step["data"]["summary"] for step in response.json()["step_runs"][:2]] == ["attempt 1", "attempt 2"]


def test_receipt_identity_preserves_distinct_operations_and_legacy_counts(tmp_path):
    from trade_compass_agent.runtime.background_review import _read_review_receipts, _combine_review_results
    config = AppConfig(data_dir=tmp_path, memory_dir=tmp_path / "vault")
    path = tmp_path / "agent_sessions" / "archive.jsonl"
    path.parent.mkdir()
    records = []
    for name in ("one", "two"):
        records.extend([
            {"role": "assistant", "tool_calls": [{"id": name, "function": {
                "name": "skill_manage", "arguments": json.dumps({"action": "archive", "name": name})}}]},
            {"role": "tool", "tool_call_id": name, "content": '{"ok":true,"changed":true,"disposition":"archived"}'},
        ])
    path.write_text("\n".join(json.dumps(record) for record in records))
    current = _read_review_receipts(config, "archive", "归档完成")
    legacy = {**current, "commits": [{k: v for k, v in receipt.items() if k != "receipt_id"}
                                    for receipt in current["commits"]]}
    merged = _combine_review_results(legacy, current)
    assert len(merged["commits"]) == 2 and merged["commits"] == current["commits"]
    assert _combine_review_results(merged, current) == merged
    assert len(_combine_review_results(legacy, current, new_attempt=True)["commits"]) == 4
