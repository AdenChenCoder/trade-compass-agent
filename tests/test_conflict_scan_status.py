import json
from dataclasses import replace

import pytest

from trade_compass_agent.config import AppConfig, LLMConfig
from trade_compass_agent.memory.memory_store import MemoryStore
from trade_compass_agent.runtime.tools.operations import tool_run_builtin_operation


def test_conflict_scan_repairs_json_once_and_keeps_original_entries(tmp_path):
    from trade_compass_agent.memory.contradiction import scan_active_conflicts

    store = MemoryStore(tmp_path)
    store.add("独立成交证据不足时仍须保留候选。", source="promotion", confidence=.9)
    before = [(m.entry_id, m.version, m.text) for m in store.list_active()]
    calls = []

    def judge(system, user):
        calls.append(user)
        return '[{"verdict":"KEEP" "reason":"无冲突"}]' if len(calls) == 1 else "[]"

    assert scan_active_conflicts(store.list_active(), "核对真实成交", "", judge) == []
    assert len(calls) == 2 and "validation_error" in calls[1]
    assert before == [(m.entry_id, m.version, m.text) for m in MemoryStore(tmp_path).list_active()]


@pytest.mark.parametrize("weekly", [False, True])
@pytest.mark.parametrize("outcome", [
    "empty", "timeout", "invalid_json", "wrong_shape", "invalid_item", "unknown_verdict", "verify_timeout", "verify_wrong_shape",
])
def test_failed_conflict_review_is_not_published_as_no_conflicts(
    tmp_path, monkeypatch, weekly, outcome
):
    from trade_compass_agent.memory import semantic_merge
    from trade_compass_agent.runtime.tools import builtin_operations
    from trade_compass_agent.runtime.workflows import engine

    config = AppConfig(data_dir=tmp_path / "data", memory_dir=tmp_path / "memory",
                       llm=LLMConfig(provider="disabled"))
    config.data_dir.mkdir()
    store = MemoryStore(config.memory_dir)
    store.add("真实成交结果不足时，保留待验证结论。", source="promotion", confidence=.9)
    before = [(m.entry_id, m.version, m.text, m.status) for m in store.get_entries_with_meta()]
    calls = []

    def judge(*args):
        calls.append(args)
        if outcome.startswith("verify_"):
            if len(calls) == 1:
                entry = store.list_active()[0]
                return json.dumps([{"verdict": "SUPERSEDE", "entry_prefix": entry.text[:10], "conflicts_with": entry.text[:10],
                    "refined": "无需真实成交结果也可认定策略有效。", "reason": "注入待复核提案"}])
            if outcome == "verify_timeout":
                raise TimeoutError("injected independent review timeout")
            return '{"reviews":[]}'
        if outcome == "timeout":
            raise TimeoutError("injected generation timeout")
        return {"empty": "[]", "invalid_json": "not JSON", "wrong_shape": "{}",
                "invalid_item": "[null]", "unknown_verdict": '[{"verdict":"DELETE"}]'}[outcome]

    monkeypatch.setattr(builtin_operations, "_memory_llm_call", judge)
    monkeypatch.setattr(semantic_merge, "maintain_memory", lambda *a, **k: {"ok": True})
    result = json.loads(tool_run_builtin_operation(config, "builtin.curate_knowledge", {
        "job_id": "weekly" if weekly else "postmarket", "as_of": "2026-10-21",
    }))
    failed = outcome != "empty"
    assert len(calls) == (2 if outcome.startswith("verify_") or outcome in {"invalid_json", "wrong_shape"} else 1)
    assert result["data"]["conflicts_applied"] == 0
    assert result["data"]["conflict_scan_status"] == ("pending" if failed else "completed")
    assert result["data"]["degraded"] == failed
    assert bool(result["data"]["warnings"]) == failed
    assert before == [(m.entry_id, m.version, m.text, m.status)
                      for m in MemoryStore(config.memory_dir).get_entries_with_meta()]

    original = engine.load_workflow_assets()["weekend_review" if weekly else "postmarket_archive"]
    step = next(s for s in original.steps if s.uses == "tool:builtin.curate_knowledge")
    primary = next(s for s in original.steps if s.primary_output)
    manifest = replace(original, steps=(
        replace(step, depends_on=()), replace(primary, depends_on=(step.id,)),
    ))

    def run_step(current, *args, **kwargs):
        data = result if current.id == step.id else {"message": "Other review completed", "data": {}}
        return engine.WorkflowStepResult(
            current.id, current.type, current.uses, data,
            warnings=tuple(data["data"].get("warnings", [])),
        )

    monkeypatch.setattr(engine, "_run_step", run_step)
    engine.run_workflow_asset(manifest, {"as_of": "2026-10-21"},
                              config=config, run_id="conflict-consumer")
    record = json.loads((config.data_dir / "workflow_runs/conflict-consumer/run.json").read_text())
    assert record["status"] == ("degraded" if failed else "completed")
    assert bool(record["warnings"]) == failed
