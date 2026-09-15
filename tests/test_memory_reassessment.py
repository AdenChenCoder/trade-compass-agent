"""Consumer regressions for reopening history without restoring unverified claims."""
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import json

import pytest

from scripts.migrate_memory_lifecycle import reopen_stale_history
from trade_compass_agent.memory.contradiction import apply_conflict_reports, scan_active_conflicts
from trade_compass_agent.memory.lineage import memory_lineage
from trade_compass_agent.memory.memory_store import EntryMeta, MemoryStore, _content_hash
from trade_compass_agent.memory.observation_store import ObservationStore
from trade_compass_agent.memory.reassessment import review_candidates
from trade_compass_agent.memory.rules_store import RulesStore
from trade_compass_agent.memory.semantic_merge import maintain_memory
from trade_compass_agent.runtime.tools.self_improve import tool_memory_write
from trade_compass_agent.web.api import _memory_response


def model(decision="admit", basis="principle", refs=None, content=None, inspect=None):
    def call(system, user):
        payload = json.loads(user)
        if inspect:
            inspect(system, payload)
        if "proposals" in payload:
            return json.dumps({"reviews": [{"entry_id": v["entry_id"], "version": v["version"],
                "valid": True, "reason": "已核对具体依据和条件"} for v in payload["proposals"]]})
        return json.dumps({"reviews": [{"entry_id": m["entry_id"], "version": m["version"],
            "decision": decision, "basis": basis, "refs": refs if refs is not None else ["grounding"],
            "content": content or m["text"], "reason": "所给交易制度要求核验行情时效。"}
            for m in payload["candidates"]]}, ensure_ascii=False)
    return call


def legacy(tmp_path):
    old = EntryMeta(text="交易决策应使用时效可验证的行情", source="curator", confidence=0,
        status="archived", reason="legacy_archived",
        last_accessed=(datetime.now(timezone.utc) - timedelta(days=200)).isoformat())
    other = EntryMeta(text="已被证伪的旧记忆", source="curator", status="archived", reason="disproved")
    (tmp_path / ".memory_meta.json").write_text(json.dumps({"schema_version": 3, "revision": 1,
        "memory": [asdict(old), asdict(other)], "user": [], "history": []}))
    evidence = tmp_path / "audit.json"
    evidence.write_text(json.dumps({old.entry_id: {"text": old.text, "stale": [{"path": "old.log", "line": 4,
        "text": "Archived stale memory (confidence=0.01): " + old.text}]}, other.entry_id: {"text": other.text, "stale": []}}))
    return MemoryStore(tmp_path), old, other, evidence


def test_reopen_review_restart_and_api_keep_history_without_quota_increase(tmp_path):
    store, old, other, evidence = legacy(tmp_path)
    assert len(reopen_stale_history(store, evidence)) == 1
    assert reopen_stale_history(store, evidence) == []
    assert not store.format_for_system_prompt()
    before = _memory_response("memory", MemoryStore(tmp_path))
    assert before.active_count == 0 and before.candidate_count == 1 and before.char_limit == 3000
    original = next(m for m in store.get_entries_with_meta(include_history=True) if m.entry_id == old.entry_id and m.version == 1)
    assert original.reason == "legacy_archived"
    result = json.loads(tool_memory_write(store, "maintain", llm_call=model()))
    assert result["ok"] and result["reviews"][0]["decision"] == "admit"
    restart = MemoryStore(tmp_path)
    assert old.text in restart.format_for_system_prompt()
    rows = restart.get_entries_with_meta(include_history=True)
    trace = memory_lineage(rows)[old.entry_id, 1]
    assert [m["version"] for m in trace["successors"]] == [2, 3]
    assert trace["successors"][-1]["status"] == "active"
    assert next(m for m in rows if m.entry_id == other.entry_id).status == "archived"
    active = restart.list_active()[0]
    assert active.reviewed_at and active.last_accessed == old.last_accessed
    assert active.review_evidence["grounding"]["text"]
    assert not restart.archive_stale() and not restart.archive_inactive()
    assert not restart.list_active()[0].needs_review
    assert reopen_stale_history(restart, evidence) == []


def test_allowlist_is_validated_before_any_reopening_and_pins_are_protected(tmp_path):
    store, old, other, evidence = legacy(tmp_path)
    data = json.loads(evidence.read_text())
    data[other.entry_id] = {"text": other.text, "stale": data[old.entry_id]["stale"]}
    evidence.write_text(json.dumps(data))
    before = (tmp_path / ".memory_meta.json").read_bytes()
    with pytest.raises(ValueError):
        reopen_stale_history(store, evidence)
    assert (tmp_path / ".memory_meta.json").read_bytes() == before
    assert not store.reopen_for_review(entry_id=old.entry_id, expected_version=1,
        actor="agent", reason="try", evidence=["request"])["ok"]


def test_existing_low_trust_drafts_reach_ai_with_full_rules_and_pins(tmp_path):
    store = MemoryStore(tmp_path)
    RulesStore(tmp_path).add("账户不得使用杠杆", actor="user")
    store.add("用户固定的长期原则", source="user_pin")
    store.add("行情过期先重新获取", source="scheduler")
    calls = []
    def inspect(system, payload):
        calls.append(payload)
        assert payload["references"]["rules"]["text"] == "账户不得使用杠杆"
        assert payload["active_memory"][0]["pinned"]
    result = maintain_memory(store, model(inspect=inspect))
    assert result["ok"] and len(calls) == 2
    assert len(MemoryStore(tmp_path).list_active()) == 2


def test_insufficient_evidence_deferred_curator_cannot_bypass_into_capacity_admission(tmp_path):
    store, old, _, evidence = legacy(tmp_path)
    reopen_stale_history(store, evidence)
    result = maintain_memory(store, model(basis="empirical", refs=[]))
    assert result["ok"] and not store.list_active()
    assert result["reviews"][0]["decision"] == "defer"
    def unexpected(*args):
        pytest.fail("Unchanged candidate was reviewed again or bypassed to capacity admission")
    assert maintain_memory(store, unexpected)["ok"]
    assert next(m for m in store.get_entries_with_meta() if m.entry_id == old.entry_id).status == "candidate"


def test_new_independent_observations_reopen_deferred_review(tmp_path):
    store = MemoryStore(tmp_path / "vault")
    store.add("量价背离应先验证成交量再评估趋势", source="scheduler")
    observations = ObservationStore(tmp_path / "observations.db")
    observations.append("session1", "analyze", "量价背离应先验证成交量再评估趋势，案例一")
    def evidence_model(system, user):
        refs = [r for r in json.loads(user)["references"] if r.startswith("observation:")]
        return model(basis="empirical", refs=refs)(system, user)
    assert review_candidates(store, evidence_model, observations=observations)["reviews"][0]["decision"] == "defer"
    observations.append("session2", "analyze", "量价背离应先验证成交量再评估趋势，独立案例二")
    result = review_candidates(store, evidence_model, observations=observations)
    assert result["reviews"][0]["decision"] == "admit" and len(store.list_active()) == 1
    assert len(store.list_active()[0].source_obs_ids) == 2
    assert all(o.recall_count == 0 for o in observations.recent())


def test_reassessment_uses_existing_quality_gate_and_admission_threshold(tmp_path):
    store = MemoryStore(tmp_path / "quality")
    store.add("今天现价123元的涨势明显")
    assert review_candidates(store, model())["ok"]
    assert not store.list_active() and "时效性" in store.get_entries_with_meta()[0].reason
    strict = MemoryStore(tmp_path / "strict", min_inject_confidence=.9)
    strict.add("下单前须校验实时行情")
    result = review_candidates(strict, model())
    assert result["ok"]
    assert result["reviews"][0]["decision"] == "defer"
    assert result["reviews"][0]["status"] == "candidate"
    assert not strict.list_active()


def test_incomplete_citation_can_be_corrected_without_guessing_evidence(tmp_path):
    store = MemoryStore(tmp_path)
    store.add("交易前需要核实市场报价时效")
    calls = []
    def evaluate(system, user):
        payload = json.loads(user)
        calls.append(payload)
        return model(refs=["grounding" if "citation_error" in payload else "ground"])(system, user)
    assert review_candidates(store, evaluate)["ok"]
    assert len(calls) == 3 and len(store.list_active()) == 1
    assert store.list_active()[0].evidence == ["grounding"]


def test_skill_loading_and_memory_retrieval_are_not_independent_market_cases(tmp_path):
    store = MemoryStore(tmp_path / "vault")
    text = "跌停与死叉同时出现应直接清仓"
    store.add(text)
    observations = ObservationStore(tmp_path / "observations.db")
    observations.append("s1", "load_skill", text + "，技能已有此参数")
    observations.append("s2", "search_memory", text + "，找到了从前的总结")
    refs = ["observation:" + o.id for o in observations.recent()]
    result = review_candidates(store, model(basis="empirical", refs=refs), observations=observations)
    assert result["ok"] and result["reviews"][0]["decision"] == "defer"
    assert not store.list_active()


def test_independent_review_catches_generalization_from_one_fault(tmp_path):
    store = MemoryStore(tmp_path)
    store.add("last_price 等于 avg_cost 即判为陈旧价，禁用。")
    def evaluate(system, user):
        payload = json.loads(user)
        if "proposals" in payload:
            return json.dumps({"reviews": [{"entry_id": v["entry_id"], "version": v["version"],
                "valid": False, "reason": "实时价格也可能等于成本价；必须结合时间戳和来源校验。"}
                for v in payload["proposals"]]})
        return model()(system, user)
    assert review_candidates(store, evaluate)["ok"]
    assert not MemoryStore(tmp_path).format_for_system_prompt()
    assert "实时价格也可能等于成本价" in store.get_entries_with_meta()[0].reason


def test_draft_created_between_batches_remains_eligible_for_next_review(tmp_path):
    store = MemoryStore(tmp_path)
    for i in range(9):
        store.add(f"待查证的不同交易原则{i}")
    calls = 0
    def evaluate(system, user):
        nonlocal calls
        calls += 1
        if calls == 2:
            store.add("审查期间出现的新候选")
        return model(decision="defer", refs=[])(system, user)
    result = review_candidates(store, evaluate)
    assert not result["ok"] and len(result["commits"]) == 8
    assert not store.maintenance_marker("candidate_reviewed_fingerprint")
    result = review_candidates(store, model(decision="defer", refs=[]))
    assert result["ok"] and len(result["reviews"]) == 10


@pytest.mark.parametrize("failure", ["timeout", "unknown_reference", "empty_reason", "rules_changed", "memory_changed"])
def test_bad_evaluation_is_retryable_and_never_promotes_or_archives(tmp_path, failure):
    store = MemoryStore(tmp_path)
    created = store.add("行情必须核验时效", source="scheduler")
    def fail(system, user):
        if failure == "timeout":
            raise TimeoutError("provider unavailable")
        result = json.loads(model()(system, user))
        if failure == "unknown_reference":
            result["reviews"][0]["refs"] = ["invented"]
        if failure == "empty_reason":
            result["reviews"][0]["reason"] = ""
        if failure == "rules_changed":
            RulesStore(tmp_path).add("新规则", actor="user")
        if failure == "memory_changed":
            store.add("并发新增的候选")
        return json.dumps(result)
    assert not review_candidates(store, fail)["ok"]
    row = next(m for m in store.get_entries_with_meta() if m.entry_id == created["entry_id"])
    assert row.status == "candidate" and row.version == 1
    assert not store.maintenance_marker("candidate_reviewed_fingerprint")
    assert review_candidates(store, model())["ok"]


def test_reviewed_candidate_cannot_evict_pinned_memory_for_capacity(tmp_path):
    store = MemoryStore(tmp_path, memory_char_limit=10)
    store.add("P" * 10, source="user_pin")
    draft = store.add("需要有效行情")
    result = review_candidates(store, model())
    assert result["ok"]
    review = result["reviews"][0]
    assert review["decision"] == "defer" and review["proposed_decision"] == "admit"
    assert review["status"] == "candidate" and review["reason"] == "capacity_review_required"
    candidate = next(m for m in store.get_entries_with_meta() if m.entry_id == draft["entry_id"])
    assert candidate.status == "candidate" and candidate.reason == "capacity_review_required"
    assert store.capacity()["chars_used"] == 10 and store.list_active()[0].source == "user_pin"


def test_review_receipt_distinguishes_merge_from_admission_or_retirement(tmp_path):
    store = MemoryStore(tmp_path)
    retained = store.add("交易前校验行情时效", source="user_pin")
    draft = store.add("拟修订的行情校验原则")
    result = review_candidates(store, model(content="交易前校验行情时效"))
    assert result["ok"]
    assert result["reviews"][0]["decision"] == "merge"
    assert result["reviews"][0]["proposed_decision"] == "admit"
    assert result["reviews"][0]["status"] == "archived"
    assert result["commits"][0]["disposition"] == "merged"
    rows = MemoryStore(tmp_path).get_entries_with_meta(include_history=True)
    trace = memory_lineage(rows)[draft["entry_id"], 1]
    assert trace["successors"][-1]["entry_id"] == retained["entry_id"]
    assert len(store.list_active()) == 1 and store.list_active()[0].source == "user_pin"


@pytest.mark.parametrize("state", ["reopened", "deferred", "below_threshold"])
def test_agent_revision_cannot_bypass_candidate_reassessment(tmp_path, state):
    if state == "below_threshold":
        store = MemoryStore(tmp_path, min_inject_confidence=.9)
        store.add("下单前须校验实时行情")
        assert review_candidates(store, model())["ok"]
    else:
        store, _, _, evidence = legacy(tmp_path)
        reopen_stale_history(store, evidence)
        if state == "deferred":
            assert review_candidates(store, model(basis="empirical", refs=[]))["ok"]
    row = next(m for m in store.get_entries_with_meta() if m.status == "candidate")
    before = (tmp_path / ".memory_meta.json").read_bytes()
    def unexpected(*args):
        pytest.fail("Unreviewed candidates must not enter the trusted revision evaluator")
    result = json.loads(tool_memory_write(store, "revise", content=row.text,
        reason="调整表达", evidence=["self:assertion"], entry_id=row.entry_id,
        expected_version=row.version, llm_call=unexpected))
    assert not result["ok"] and result["disposition"] == "pending"
    assert (tmp_path / ".memory_meta.json").read_bytes() == before
    assert not MemoryStore(tmp_path).format_for_system_prompt()


def test_revision_can_admit_verified_candidate_after_capacity_is_freed(tmp_path):
    store = MemoryStore(tmp_path, memory_char_limit=20)
    pin = store.add("P" * 20, source="user_pin")
    store.add("下单前须校验实时行情")
    assert review_candidates(store, model())["ok"]
    row = next(m for m in store.get_entries_with_meta() if m.status == "candidate")
    assert row.reason == "capacity_review_required"
    store.archive_entry(entry_id=pin["entry_id"], actor="user", reason="用户取消固定内容")
    result = json.loads(tool_memory_write(store, "revise", content=row.text,
        reason="已完成复评且额度已释放", evidence=row.evidence, entry_id=row.entry_id,
        expected_version=row.version, llm_call=lambda *args: '{"valid":true,"reason":"保持已验证内容"}'))
    assert result["ok"] and result["accepted"]
    restarted = MemoryStore(tmp_path, memory_char_limit=20)
    assert row.text in restarted.format_for_system_prompt()
    assert [m.text for m in restarted.list_active()] == [row.text]


def test_revision_rejects_user_rule_changes_during_evaluation(tmp_path):
    store = MemoryStore(tmp_path)
    row = store.add("下单前校验行情时效", source="promotion")
    rules = RulesStore(tmp_path)
    rules.add("按原有用户约束审查", actor="user")
    before = (tmp_path / ".memory_meta.json").read_bytes()
    def evaluate(system, user):
        assert "按原有用户约束审查" in system
        rules.add("暂时禁止采纳本条交易原则", actor="user")
        return '{"valid":true,"reason":"旧规则允许更新"}'
    result = json.loads(tool_memory_write(store, "revise", content="执行交易前必须校验行情时效",
        reason="补充描述", evidence=["grounding"], entry_id=row["entry_id"],
        expected_version=row["version"], llm_call=evaluate))
    assert not result["ok"] and result["disposition"] == "version_conflict"
    assert (tmp_path / ".memory_meta.json").read_bytes() == before
    assert MemoryStore(tmp_path).list_active()[0].text == "下单前校验行情时效"


def test_retirement_requires_reason_and_curator_sees_complete_rules_and_text(tmp_path):
    store = MemoryStore(tmp_path)
    text = "条件和例外" * 60
    row = store.add(text, source="curator")
    RulesStore(tmp_path).add("用户规则优先", actor="user")
    assert not store.archive_entry(entry_id=row["entry_id"], reason=" ")["ok"]
    def judge(system, prompt):
        assert text in prompt and "用户规则优先" in prompt
        return json.dumps([{"verdict": "ARCHIVE", "entry_prefix": text[:20], "reason": "长期未访问"}])
    assert scan_active_conflicts(store.list_active(), "约束", "", judge, user_rules="用户规则优先") == []
    def conflict(*args):
        return json.dumps([{"verdict": "ARCHIVE", "entry_prefix": text[:20], "reason": "违反具体规则", "archive_basis": "contradiction"}])
    reports = scan_active_conflicts(store.list_active(), "约束", "", conflict, user_rules="用户规则优先")
    RulesStore(tmp_path).add("后来新增规则", actor="user")
    assert not apply_conflict_reports(reports, store)
    assert len(store.list_active()) == 1


def test_legacy_hash_lineage_and_timestamp_repair_is_exact_and_idempotent(tmp_path):
    old = EntryMeta(text="原版本", source="legacy", status="archived", reason="legacy_superseded")
    new = EntryMeta(text="新版本", source="curator")
    ledger = {"schema_version": 3, "revision": 5, "memory": [asdict(new)], "user": [],
              "history": [{**asdict(old), "target": "memory"}]}
    (tmp_path / ".memory_meta.json").write_text(json.dumps(ledger))
    (tmp_path / ".memory-migration-v2.json").write_text(json.dumps({"metadata": {"_superseded": [
        {"text": old.text, "superseded_by": _content_hash(new.text), "superseded_at": "2026-01-02"}]}}))
    store = MemoryStore(tmp_path)
    rows = store.get_entries_with_meta(include_history=True)
    assert next(m for m in rows if m.entry_id == old.entry_id).retired_at == "2026-01-02"
    assert memory_lineage(rows)[old.entry_id, 1]["successors"][0]["text"] == new.text
    before = (tmp_path / ".memory_meta.json").read_bytes()
    MemoryStore(tmp_path)
    assert (tmp_path / ".memory_meta.json").read_bytes() == before
