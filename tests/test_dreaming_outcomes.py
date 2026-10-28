import json

import pytest

from trade_compass_agent.config import AppConfig, LLMConfig, SchedulerConfig
from trade_compass_agent.memory.promotion import PromotionResult
from trade_compass_agent.runtime.tools.operations import tool_run_builtin_operation
from trade_compass_agent.runtime.exceptions import AgentTurnError


@pytest.mark.parametrize("weekly", [False, True])
@pytest.mark.parametrize("verdict,expected", [("ARCHIVE", 0), ("REJECT", 0), ("KNOWLEDGE", 1)])
@pytest.mark.parametrize("failure", ["", "procedure", "diary"])
def test_operation_reports_only_adopted_memory_as_promoted(tmp_path, monkeypatch, weekly, verdict, expected, failure):
    from trade_compass_agent.memory import dream_diary, insights, patterns, promotion, time_tree

    config = AppConfig(data_dir=tmp_path / "data", memory_dir=tmp_path / "memory",
                       llm=LLMConfig(provider="disabled"), scheduler=SchedulerConfig(timezone="UTC"))
    config.data_dir.mkdir()
    config.memory_dir.mkdir()
    monkeypatch.setattr(time_tree.TimeTree, "seal_day", lambda *a, **k: None)
    monkeypatch.setattr(time_tree.TimeTree, "maybe_cascade", lambda *a, **k: None)
    monkeypatch.setattr(time_tree.TimeTree, "get_node", lambda *a, **k: None)
    monkeypatch.setattr(time_tree.TimeTree, "nodes_by_level", lambda *a, **k: [])
    from types import SimpleNamespace
    pattern_calls = []
    def discover(*args, **kwargs):
        pattern_calls.append(kwargs)
        return [SimpleNamespace(theme="研究流程", strength=.8, days_seen=2,
            description="重复研究，不是实战验证", evidence=[])] if failure == "procedure" else []
    monkeypatch.setattr(patterns, "discover_patterns", discover)
    monkeypatch.setattr(patterns, "persist_patterns", lambda *a, **k: None)
    if failure == "procedure":
        monkeypatch.setattr(dream_diary, "run_procedure_extraction",
                            lambda *a, **k: (_ for _ in ()).throw(AgentTurnError("output limit")))
    monkeypatch.setattr(insights, "generate_insights", lambda *a, **k: [])
    monkeypatch.setattr(promotion, "rank_promotion_candidates", lambda *a, **k: [])
    monkeypatch.setattr(promotion, "apply_promotions", lambda *a, **k: [
        PromotionResult(verdict=verdict, refined_text="待验证规律", reason="fixture verdict")])
    captured = []
    def diary(config, summary, **kwargs):
        captured.append(summary)
        if failure == "diary":
            raise AgentTurnError("diary failed")
        return "学习日记"

    monkeypatch.setattr(dream_diary, "run_dream_diary", diary)

    name = "builtin.run_weekly_dreaming" if weekly else "builtin.run_dreaming"
    result = json.loads(tool_run_builtin_operation(config, name, {
        "job_id": "weekly" if weekly else "postmarket", "as_of": "2026-10-21"}))
    assert pattern_calls[0]["timezone_name"] == "UTC"
    assert pattern_calls[0]["as_of"].isoformat() == "2026-10-21"
    assert result["data"]["promoted"] == expected
    assert f"{expected} promoted" in result["message"]
    assert f"已采纳 {expected}" in captured[0]
    assert bool(result["data"].get("degraded")) == bool(failure)
    if failure == "procedure":
        assert "流程提炼" in result["data"]["error"]
    if failure == "diary":
        assert "学习日记" in result["data"]["error"]

    # The actual operation result must reach the persisted workflow surface.
    from dataclasses import replace
    from trade_compass_agent.runtime.workflows import engine

    asset = "weekend_review" if weekly else "postmarket_archive"
    original = engine.load_workflow_assets()[asset]
    step = next(s for s in original.steps if s.primary_output)
    manifest = replace(original, steps=(replace(step, depends_on=()),))
    monkeypatch.setattr(engine, "_run_step", lambda step, *a, **k: engine.WorkflowStepResult(
        step.id, step.type, step.uses, result,
    ))
    output = engine.run_workflow_asset(manifest, {"as_of": "2026-10-21"},
        config=config, run_id="dreaming-consumer")
    record = json.loads((config.data_dir / "workflow_runs/dreaming-consumer/run.json").read_text())
    assert bool(output.get("degraded")) == bool(failure)
    assert record["status"] == ("degraded" if failure else "completed")


def test_rejected_and_archived_verdicts_never_count_as_adopted():
    from trade_compass_agent.memory.promotion import count_promoted_memories

    results = [PromotionResult(v, "content", "reason")
               for v in ["KNOWLEDGE", "USER", "SUPERSEDE", "ARCHIVE", "REJECT", "SKIP"]]
    assert count_promoted_memories(results) == 3


@pytest.mark.parametrize("timezone_name,expected", [
    ("Asia/Shanghai", {"overnight", "before_midnight", "legacy"}),
    ("UTC", {"before_midnight", "next_local_day", "legacy"}),
])
def test_daily_archive_sources_use_scheduler_calendar(tmp_path, monkeypatch, timezone_name, expected):
    from trade_compass_agent.memory import dream_diary, insights, patterns, promotion, time_tree
    from trade_compass_agent.memory.observation_store import ObservationStore
    from trade_compass_agent.memory.session_summary_store import SessionSummaryStore
    from trade_compass_agent.runtime.tools import builtin_operations

    config = AppConfig(data_dir=tmp_path / "data", memory_dir=tmp_path / "memory",
                       llm=LLMConfig(provider="disabled"), scheduler=SchedulerConfig(timezone=timezone_name))
    config.data_dir.mkdir()
    config.memory_dir.mkdir()
    observations = ObservationStore(config.data_dir / "observations.db")
    sessions = SessionSummaryStore(config.data_dir / "sessions.db")
    stamps = {
        "overnight": "2026-10-20T16:30:00+00:00",
        "before_midnight": "2026-10-21T15:59:00+00:00",
        "next_local_day": "2026-10-21T16:01:00+00:00",
        "legacy": "2026-10-21T01:00:00",
    }
    for name, stamp in stamps.items():
        observations.append("session-" + name, "get_bars", name)
        observations._get_conn().execute("UPDATE observations SET created_at=? WHERE summary=?", (stamp, name))
        sessions.upsert("session-" + name, name, started_at=stamp)
    observations._get_conn().commit()
    original_observations = {o.id: o.created_at for o in observations.recent(100)}
    original_sessions = {s.session_id: s.started_at for s in sessions.recent(100)}
    monkeypatch.setattr(builtin_operations, "_summary_llm_call", lambda config, system, content: content)
    monkeypatch.setattr(time_tree.TimeTree, "maybe_cascade", lambda *a, **k: None)
    monkeypatch.setattr(patterns, "discover_patterns", lambda *a, **k: [])
    monkeypatch.setattr(insights, "generate_insights", lambda *a, **k: [])
    monkeypatch.setattr(promotion, "rank_promotion_candidates", lambda *a, **k: [])
    monkeypatch.setattr(promotion, "apply_promotions", lambda *a, **k: [])
    monkeypatch.setattr(dream_diary, "run_dream_diary", lambda *a, **k: "学习日记")

    result = json.loads(tool_run_builtin_operation(config, "builtin.run_dreaming", {
        "job_id": "postmarket", "as_of": "2026-10-21"}))
    assert result["data"]["diary"] and not result["data"].get("degraded")
    assert "## 2026-10-21" in (config.memory_dir / "DREAM_DIARY.md").read_text()
    tree = time_tree.TimeTree(config.data_dir / "time_tree.db")
    node = tree.get_node("2026-10-21")
    assert node.source_count == len(expected) * 2
    sources = tree._get_conn().execute(
        "SELECT source_type,source_id FROM time_node_sources WHERE node_id=?", (node.id,)).fetchall()
    assert {sid for kind, sid in sources if kind == "session"} == {"session-" + n for n in expected}
    expected_ids = {o.id for o in observations.recent(100) if o.summary in expected}
    assert {sid for kind, sid in sources if kind == "observation"} == expected_ids
    assert original_observations == {o.id: o.created_at for o in observations.recent(100)}
    assert original_sessions == {s.session_id: s.started_at for s in sessions.recent(100)}


@pytest.mark.parametrize("summary", [False, True])
def test_direct_memory_calls_bind_agent_budget_and_preserve_parent_cancel(tmp_path, monkeypatch, summary):
    import time
    from types import SimpleNamespace
    from trade_compass_agent.llm import providers
    from trade_compass_agent.runtime.tools import builtin_operations

    config = AppConfig(data_dir=tmp_path / "data", memory_dir=tmp_path / "memory",
                       llm=LLMConfig(provider="disabled", agent_timeout=17))
    seen = []

    class Client:
        def complete(self, messages):
            cancel = providers._request_cancelled.get()
            seen.append((providers._request_deadline.get(), bool(cancel and cancel()),
                         providers._maintenance_mode.get()))
            return SimpleNamespace(content="[]")

    monkeypatch.setattr(providers, "create_chat_client", lambda config: Client())
    call = builtin_operations._summary_llm_call if summary else builtin_operations._memory_llm_call
    before = time.monotonic()
    assert call(config, "system", "evidence") == "[]"
    after = time.monotonic()
    assert seen[0][0] is not None and before + 17 <= seen[0][0] <= after + 17
    assert seen[0][1:] == (False, summary)
    parent_deadline = time.monotonic() + 5
    with providers.llm_request_deadline(parent_deadline, is_cancelled=lambda: True):
        assert call(config, "system", "evidence") == "[]"
    assert seen[1] == (parent_deadline, True, summary)
    assert providers._request_deadline.get() is None
    assert not providers._maintenance_mode.get()
