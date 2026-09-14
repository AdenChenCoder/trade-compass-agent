"""Consumer regressions for bounded, durable, autonomous memory revisions."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import json
import subprocess
import sys

import pytest

from trade_compass_agent.memory.memory_store import ENTRY_DELIMITER, EntryMeta, MemoryStore
from trade_compass_agent.memory.semantic_merge import evaluate_revision, maintain_memory
from trade_compass_agent.runtime.tools.self_improve import tool_memory_write


def test_old_instances_do_not_lose_provenance_and_never_promote_on_restart(tmp_path):
    a, b = MemoryStore(tmp_path), MemoryStore(tmp_path)
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda pair: pair[0].add(pair[1], source="scheduler"), [(a, "量价背离需要验证"), (b, "ETF轮动依赖成交深度")]))
    assert all(r["ok"] and r["status"] == "candidate" for r in results)
    reload = MemoryStore(tmp_path)
    assert {m.text for m in reload.get_entries_with_meta()} == {"量价背离需要验证", "ETF轮动依赖成交深度"}
    assert all(m.source == "scheduler" and m.confidence == .4 for m in reload.get_entries_with_meta())
    assert reload.format_for_system_prompt() == ""
    assert reload.capacity()["chars_used"] == 0
    assert len({m.entry_id for m in reload.get_entries_with_meta()}) == 2


def test_process_writers_share_the_same_boundary(tmp_path):
    code = "from pathlib import Path; from trade_compass_agent.memory.memory_store import MemoryStore; import sys; s=MemoryStore(Path(sys.argv[1])); [s.add(f'process {sys.argv[2]} observation {i}') for i in range(8)]"
    processes = [subprocess.Popen([sys.executable, "-c", code, str(tmp_path), str(i)]) for i in range(3)]
    assert all(p.wait(timeout=20) == 0 for p in processes)
    rows = MemoryStore(tmp_path).get_entries_with_meta()
    assert len(rows) == 24
    assert all(m.status == "candidate" and m.confidence == .4 for m in rows)


def test_archive_and_candidate_do_not_consume_effective_capacity(tmp_path):
    store = MemoryStore(tmp_path, memory_char_limit=30)
    a = store.add("A" * 25, source="promotion")
    assert a["chars_used"] == 25
    assert store.archive_entry(entry_id=a["entry_id"], reason="disproved")["ok"]
    store.add("B" * 300)
    adopted = store.add("C" * 30, source="promotion")
    assert adopted["accepted"] and adopted["chars_used"] == 30
    assert (tmp_path / "KNOWLEDGE.md").read_text() == "C" * 30
    assert {m.status for m in store.get_entries_with_meta()} == {"active", "candidate", "archived"}
    pending = store.add("D", source="promotion")
    assert pending["ok"] and not pending["accepted"] and pending["reason"] == "capacity_review_required"
    snapshot = MemoryStore(tmp_path, memory_char_limit=30).format_for_system_prompt()
    assert "C" * 30 in snapshot and "B" * 300 not in snapshot
    assert "D" not in ENTRY_DELIMITER.join(m.text for m in store.list_active())


def test_capacity_checked_for_pin_revival_replace_and_batch(tmp_path):
    store = MemoryStore(tmp_path, memory_char_limit=20)
    pinned = store.add("P" * 20, source="user_pin")
    draft = store.add("candidate")
    assert not store.add("candidate", source="user_pin")["ok"]
    assert not store.replace("P", "P" * 21, actor="user")["ok"]
    assert not store.commit_revision(replacements=[], content="more", reason="new evidence", evidence=["r1"])["ok"]
    assert store.capacity()["chars_used"] == 20
    assert next(m for m in store.get_entries_with_meta() if m.entry_id == draft["entry_id"]).status == "candidate"
    assert not store.archive_entry(entry_id=pinned["entry_id"], actor="agent")["ok"]


def test_old_unknown_and_mismatched_metadata_cannot_become_high_trust(tmp_path):
    (tmp_path / "KNOWLEDGE.md").write_text("unrecorded fact")
    (tmp_path / ".memory_meta.json").write_text(json.dumps({"memory": [asdict(EntryMeta(text="different fact", source="scheduler", confidence=.4))], "user": []}))
    store = MemoryStore(tmp_path)
    assert {m.text for m in store.get_entries_with_meta()} == {"different fact", "unrecorded fact"}
    assert all(m.status == "candidate" and m.confidence <= .4 for m in store.get_entries_with_meta())
    assert (tmp_path / ".memory-migration-v2.json").exists()
    assert store.format_for_system_prompt() == ""


def test_exact_duplicate_migration_preserves_pin_and_history(tmp_path):
    (tmp_path / "KNOWLEDGE.md").write_text("same fact\n§\nsame fact")
    rows = [asdict(EntryMeta(text="same fact", source="scheduler", confidence=.4)), asdict(EntryMeta(text="same fact", source="user_pin"))]
    (tmp_path / ".memory_meta.json").write_text(json.dumps({"memory": rows, "user": []}))
    store = MemoryStore(tmp_path)
    assert len(store.list_active()) == 1 and store.list_active()[0].source == "user_pin"
    assert store.capacity()["archived_count"] == 1
    assert store.capacity()["chars_used"] == len("same fact")


def test_failed_commit_and_projection_recovery(tmp_path, monkeypatch):
    store = MemoryStore(tmp_path)
    first = store.add("stable prior", source="promotion")
    original = store._atomic_write
    def fail_ledger(path, text):
        if path == store._meta_file:
            raise OSError("disk failed before commit")
        original(path, text)
    monkeypatch.setattr(store, "_atomic_write", fail_ledger)
    with pytest.raises(OSError):
        store.replace("stable", "new content", expected_version=first["version"])
    assert MemoryStore(tmp_path).list_active()[0].text == "stable prior"
    def fail_projection(path, text):
        if path == store._memory_file:
            raise OSError("projection unavailable")
        original(path, text)
    monkeypatch.setattr(store, "_atomic_write", fail_projection)
    assert store.replace("stable", "committed revised", expected_version=first["version"])["ok"]
    reload = MemoryStore(tmp_path)
    assert reload.list_active()[0].text == "committed revised"
    assert (tmp_path / "KNOWLEDGE.md").read_text() == "committed revised"
    assert any(m.text == "stable prior" for m in reload.get_entries_with_meta(include_history=True))


def test_versioned_revision_preserves_originals_on_conflict_or_bad_merge(tmp_path):
    store = MemoryStore(tmp_path)
    a = store.add("行情过期时重新获取报价，不能直接沿用旧价", source="promotion")
    b = store.add("只能在交易时段使用有效市场报价下单", source="promotion")
    refs = [{"entry_id": r["entry_id"], "version": r["version"]} for r in (a, b)]
    rejected = evaluate_revision(store, replacements=refs, content="随时沿用旧价下单", reason="merge", evidence=["a", "b"], llm_call=lambda *a: '{"valid":false,"reason":"遗漏时段与时效条件"}')
    assert not rejected["ok"] and len(store.list_active()) == 2
    version = store.revision
    store.replace("行情过期", "报价过期必须重新获取", entry_id=a["entry_id"], expected_version=a["version"])
    result = store.commit_revision(replacements=refs, content="完整版本", reason="merge", evidence=["a"], expected_revision=version)
    assert result["disposition"] == "version_conflict" and len(store.list_active()) == 2


def test_pressure_merge_two_entries_atomic_and_next_session_consumes_it(tmp_path):
    store = MemoryStore(tmp_path, memory_char_limit=47)
    a = store.add("涨停家数超过三十家可作为市场情绪偏强的参考指标", source="curator")
    b = store.add("涨停超过三十家可视为市场情绪偏强的信号", source="curator")
    assert store.capacity()["pressure"]
    def llm(system, user):
        if "审查记忆修订" in system:
            return '{"valid":true,"reason":"保留三十家阈值及参考属性"}'
        return '{"content":"涨停超过三十家是市场情绪偏强的参考信号","reason":"合并相同阈值与参考判断"}'
    result = maintain_memory(store, llm)
    assert result["ok"] and result["merged_clusters"] == 1
    assert len(store.list_active()) == 1
    assert len(store.get_entries_with_meta(include_history=True)) == 3
    assert a["entry_id"] != b["entry_id"]
    assert "参考信号" in MemoryStore(tmp_path, memory_char_limit=47).format_for_system_prompt()


def test_age_or_repeated_recall_is_not_evidence(tmp_path):
    store = MemoryStore(tmp_path)
    store.add("长期有效条件原则", source="curator")
    store._meta["memory"][0]["last_accessed"] = (datetime.now(timezone.utc) - timedelta(days=500)).isoformat()
    store._save_meta()
    assert not store.archive_stale() and not store.archive_inactive()
    assert store.list_active()[0].needs_review
    draft = store.add("待验证候选", source="curator", confidence=.45)
    for _ in range(5):
        store.reinforce("待验证候选")
    assert next(m for m in store.get_entries_with_meta() if m.entry_id == draft["entry_id"]).confidence == .45


def test_tool_allows_autonomous_retirement_and_protects_user_pin(tmp_path):
    store = MemoryStore(tmp_path)
    ordinary = store.add("条件已消失", source="promotion")
    pin = store.add("用户固定原则", source="user_pin")
    for row, allowed in ((ordinary, True), (pin, False)):
        result = json.loads(tool_memory_write(store, "remove", actor="agent", entry_id=row["entry_id"],
            expected_version=row["version"], reason="条件失效", evidence=["task:checked"]))
        assert result["ok"] is allowed
    assert store.list_active()[0].source == "user_pin"


def test_api_snapshot_distinguishes_effective_candidate_and_history(client, tmp_path, monkeypatch):
    from trade_compass_agent.web import api
    store = MemoryStore(tmp_path)
    a = store.add("当前有效原则", source="promotion")
    store.add("等待验证的新发现")
    b = store.add("已经失效的条件", source="promotion")
    store.archive_entry(entry_id=b["entry_id"], reason="条件已证伪")
    monkeypatch.setattr(api, "_memory_store_for_api", lambda: (store, None))
    reply = client.get("/api/memory/memory")
    assert reply.status_code == 200
    payload = reply.json()
    assert payload["chars_used"] == len("当前有效原则") and payload["char_limit"] == 3000
    assert payload["active_count"] == 1 and payload["candidate_count"] == 1 and payload["archived_count"] >= 1
    assert any(r["entry_id"] == a["entry_id"] and r["status"] == "active" for r in payload["entries"])
    assert any(r["text"] == "已经失效的条件" and r["status"] == "archived" for r in payload["entries"])


def test_corruption_cannot_silently_drop_a_recent_pin(tmp_path):
    store = MemoryStore(tmp_path)
    store.add("ordinary")
    store.add("recent user pin", source="user_pin")
    (tmp_path / ".memory_meta.json").write_text("broken")
    with pytest.raises(ValueError, match="verified backup"):
        MemoryStore(tmp_path)
    assert "recent user pin" in (tmp_path / "KNOWLEDGE.md").read_text()


def test_capacity_blocked_promotion_retains_new_evidence_for_comparison(tmp_path):
    store = MemoryStore(tmp_path, memory_char_limit=15)
    store.add("A" * 15, source="promotion")
    candidate = store.add("new observation")
    promoted = store.add("new observation", source="promotion", meta_extra={"source_obs_ids": ["one", "two"]})
    assert promoted["ok"] and not promoted["accepted"] and promoted["entry_id"] == candidate["entry_id"]
    row = next(m for m in store.get_entries_with_meta() if m.entry_id == candidate["entry_id"])
    assert row.source == "promotion" and row.reason == "capacity_review_required" and row.source_obs_ids == ["one", "two"]
    assert not store.add("new pinned principle", source="user_pin")["ok"]
    assert store.capacity()["chars_used"] == 15


def test_failed_pressure_evaluation_is_retryable_and_does_not_claim_completion(tmp_path):
    store = MemoryStore(tmp_path, memory_char_limit=47)
    store.add("涨停家数超过三十家可作为市场情绪偏强的参考指标", source="curator")
    store.add("涨停超过三十家可视为市场情绪偏强的信号", source="curator")
    def unavailable(*args):
        raise TimeoutError("model unavailable")
    result = maintain_memory(store, unavailable)
    assert not result["ok"] and not result["changed"] and "model unavailable" in result["error"]
    assert store.maintenance_marker("reviewed_fingerprint") is None
    assert len(store.list_active()) == 2


@pytest.mark.parametrize("failure", ["evaluation_timeout", "proposal_timeout", "rejected"])
def test_completed_merge_receipt_survives_later_admission_failure(tmp_path, failure):
    from trade_compass_agent.memory.semantic_merge import maintain_memory
    store = MemoryStore(tmp_path, memory_char_limit=47)
    store.add("涨停家数超过三十家可作为市场情绪偏强的参考指标", source="curator")
    store.add("涨停超过三十家可视为市场情绪偏强的信号", source="curator")
    candidate = store.add("基金净值溢价需要单独核对", source="promotion")
    assert not candidate["accepted"]
    checks = 0
    def llm(system, prompt):
        nonlocal checks
        if "审查记忆修订" in system:
            checks += 1
            if checks == 2:
                if failure == "rejected":
                    return json.dumps({"valid": False, "reason": "admission rejected"})
                raise TimeoutError("admission evaluator unavailable")
            return json.dumps({"valid": True, "reason": "保留三十家阈值和参考属性"})
        if "核心记忆策展人" in system:
            if failure == "proposal_timeout":
                raise TimeoutError("admission proposal unavailable")
            return json.dumps({"content": "基金净值溢价需要单独核对", "reason": "已验证候选在合并后有空间",
                "evidence": ["promotion:checked"], "replacements": [{"entry_id": candidate["entry_id"], "version": candidate["version"]}]})
        return json.dumps({"content": "涨停超过三十家是市場情绪偏强的参考信号", "reason": "合并相同阈值与参考判断"})
    result = maintain_memory(store, llm)
    assert not result["ok"] and "admission" in result["error"]
    assert len(store.list_active()) == 1  # The merge was committed before the error.
    assert result.get("changed") is True and len(result.get("commits", [])) == 1, result
    assert result["merged_clusters"] == 1

    from trade_compass_agent.config import AppConfig
    from trade_compass_agent.runtime.background_review import _read_review_receipts
    config = AppConfig(data_dir=tmp_path / "data", memory_dir=tmp_path)
    trace = config.data_dir / "agent_sessions" / "partial.jsonl"
    trace.parent.mkdir(parents=True)
    trace.write_text("\n".join(json.dumps(row) for row in [
        {"role": "assistant", "tool_calls": [{"id": "maintain", "function": {
            "name": "write_knowledge", "arguments": '{"action":"maintain"}'}}]},
        {"role": "tool", "tool_call_id": "maintain", "content": json.dumps(result)},
    ]))
    visible = _read_review_receipts(config, "partial", "检查完成")
    assert visible["commits"][0]["result"] == result["commits"][0]
    assert bool(visible["errors"]) == (failure != "rejected")
