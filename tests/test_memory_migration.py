"""Repair only evidenced failed drafts; historical exclusions must survive."""
import json
from pathlib import Path
import runpy

from trade_compass_agent.memory.memory_store import MemoryStore


def test_recover_failed_draft_is_candidate_idempotent_and_does_not_replay_history(tmp_path):
    recover = runpy.run_path(str(Path(__file__).parents[1] / "scripts/migrate_memory_lifecycle.py"))["recover_capacity_rejections"]
    store = MemoryStore(tmp_path / "vault")
    old = store.add("已经停用的经验")
    store.archive_entry(entry_id=old["entry_id"], reason="已证伪")
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    rows = []
    for index, (action, content, error) in enumerate([
        ("add", "此前因为满额没能保存的新经验", "Would exceed memory limit (3031/3000 chars). Remove old entries first."),
        ("add", "已经停用的经验", "Would exceed memory limit"),
        ("remove", "不应重放的操作", "Would exceed memory limit"),
        ("add", "被安全检查拒绝的内容", "Content blocked by safety filter"),
    ]):
        rows.extend([
            {"role": "assistant", "tool_calls": [{"id": str(index), "function": {"name": "write_knowledge", "arguments": json.dumps({"action": action, "content": content})}}]},
            {"role": "tool", "tool_call_id": str(index), "content": json.dumps({"ok": False, "error": error})},
        ])
    (sessions / "scheduler-eod-review-2026-09-14.jsonl").write_text("\n".join(json.dumps(r) for r in rows))
    assert recover(store, sessions, "2026-09-13") == []
    recovered = recover(store, sessions, "2026-09-14")
    assert len(recovered) == 1
    assert recover(store, sessions, "2026-09-14") == []
    reloaded = MemoryStore(tmp_path / "vault")
    assert len(reloaded.get_entries_with_meta()) == 2
    draft = next(m for m in reloaded.get_entries_with_meta() if m.entry_id == recovered[0])
    assert draft.status == "candidate" and draft.confidence == .4 and draft.source == "scheduler"
    assert draft.reason == "recovered_capacity_rejection" and "line:2" in draft.evidence[0]
    assert reloaded.capacity()["chars_used"] == 0 and reloaded.format_for_system_prompt() == ""
    assert next(m for m in reloaded.get_entries_with_meta() if m.entry_id == old["entry_id"]).status == "archived"
