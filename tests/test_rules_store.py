from __future__ import annotations

import pytest

from trade_compass_agent.memory.rules_store import RulesStore
from trade_compass_agent.runtime.bootstrap import build_system_prompt


def test_rules_store_crud_and_agent_write_block(tmp_path) -> None:
    store = RulesStore(tmp_path)

    added = store.add("单票最大仓位不超过总资产的 20%。", actor="user")
    assert added["ok"] is True
    entry = store.list_entries()[0]

    updated = store.replace(entry.id, "单票最大仓位不超过总资产的 15%。", actor="cli")
    assert updated["ok"] is True
    assert store.list_entries()[0].id == entry.id
    assert "15%" in store.read_for_prompt()

    with pytest.raises(PermissionError):
        store.add("Agent 不可写入。", actor="agent")

    removed = store.remove(entry.id, actor="web")
    assert removed["ok"] is True
    assert store.list_entries() == []


def test_rules_store_reconciles_direct_file_edits(tmp_path) -> None:
    store = RulesStore(tmp_path)
    (tmp_path / "RULES.md").write_text("§\n禁止追涨停。\n§\n必须给出回调价。", encoding="utf-8")

    assert store.read_for_prompt() == "禁止追涨停。\n§\n必须给出回调价。"
    assert not (tmp_path / ".rules_meta.json").exists()

    entries = store.list_entries()
    assert [entry.text for entry in entries] == ["禁止追涨停。", "必须给出回调价。"]
    assert (tmp_path / ".rules_meta.json").is_file()

    version = store.version()
    (tmp_path / "RULES.md").write_text("§\n禁止追涨停。", encoding="utf-8")
    assert len(store.list_entries()) == 1
    assert store.version() != version


def test_user_rules_prompt_order(tmp_path) -> None:
    RulesStore(tmp_path).add("禁止追涨停。", actor="user")

    prompt = build_system_prompt(memory_dir=tmp_path, skills=[])
    grounding_idx = prompt.index("## 数据真实性规则")
    rules_idx = prompt.index("<user-rules")
    assert grounding_idx < rules_idx
    assert "禁止追涨停。" in prompt
    assert "mutable-by=\"human-only\"" in prompt
