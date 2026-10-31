from __future__ import annotations

import json
from pathlib import Path

import pytest

from trade_compass_agent.memory.tree.search import (
    MemorySearchIndex,
    reindex_memory_vault,
    search_memory_chunks,
)
from trade_compass_agent.memory.tree.storage import MemoryTreeStore
from trade_compass_agent.runtime.tools.memory import tool_search_memory, tool_write_memory


def test_write_and_search_memory(tmp_path: Path) -> None:
    memory_dir = tmp_path / "memory"
    result = json.loads(tool_write_memory(memory_dir, "insights", "600519 突破 MA20"))
    assert result["scope"] == "insights"
    assert (memory_dir / "tree" / "insights").is_dir()

    hits = search_memory_chunks(memory_dir, "600519")
    assert hits
    assert "600519" in hits[0].content

    payload = json.loads(tool_search_memory(memory_dir, "MA20"))
    assert payload["results"]


def test_memory_tree_store_recent(tmp_path: Path) -> None:
    store = MemoryTreeStore(tmp_path / "memory")
    store.write("session", "first note")
    store.write("session", "second note")
    chunks = store.recent_chunks(limit=2)
    assert len(chunks) == 2


def test_invalid_scope_returns_recoverable_tool_error(tmp_path: Path) -> None:
    memory_dir = tmp_path / "memory"
    result = json.loads(tool_write_memory(memory_dir, "BAD SCOPE!", "x"))
    assert result["error_type"] == "invalid_scope"
    assert result["scope"] == "BAD SCOPE!"
    assert result["content_hash"]
    assert not list(memory_dir.rglob("*.md"))


def test_invalid_scope_still_raises_at_storage_boundary(tmp_path: Path) -> None:
    memory_dir = tmp_path / "memory"
    with pytest.raises(ValueError, match="invalid memory scope:"):
        MemoryTreeStore(memory_dir).write("BAD SCOPE!", "x")
    assert not list(memory_dir.rglob("*.md"))


def test_fts_index(tmp_path: Path) -> None:
    memory_dir = tmp_path / "memory"
    store = MemoryTreeStore(memory_dir)
    chunk = store.write("qa", "ETF rotation note")
    index = MemorySearchIndex(memory_dir / "tree" / "chunks.db")
    index.index_file(chunk.scope, chunk.content, chunk.path)
    hits = index.search("ETF")
    assert hits


def test_exact_chinese_correction_precedes_loose_matches_after_reopen(tmp_path: Path) -> None:
    memory_dir = tmp_path / "memory"
    query = "预设止损价不等于已提交且已接受的止损委托"
    for _ in range(8):
        tool_write_memory(memory_dir, "trade-log", "止损：旧计划。执行：待确认。")
    receipt = json.loads(tool_write_memory(memory_dir, "trade-log", f"复核更正\n\n{query}。\n原始日记保留。"))
    for _ in range(2):
        hits = json.loads(tool_search_memory(memory_dir, query, limit=3))["results"]
        assert hits[0]["path"] == receipt["path"] and "复核更正" in hits[0]["content"]
        assert len({hit["path"] for hit in hits}) == len(hits)
        assert MemorySearchIndex(memory_dir / "tree/chunks.db").search(query, limit=1)[0].path == receipt["path"]


def test_reindex_memory_vault(tmp_path: Path) -> None:
    memory_dir = tmp_path / "memory"
    tree = memory_dir / "tree" / "insights"
    tree.mkdir(parents=True)
    (tree / "note.md").write_text("contextual take profit framework", encoding="utf-8")
    (memory_dir / "KNOWLEDGE.md").write_text("buy-sell-decision-framework 多维评估", encoding="utf-8")

    count = reindex_memory_vault(memory_dir)
    assert count == 1  # KNOWLEDGE excluded by default

    hits = search_memory_chunks(memory_dir, "framework")
    assert hits
    assert all("KNOWLEDGE" not in h.path for h in hits)

    count_with_knowledge = reindex_memory_vault(memory_dir, index_knowledge=True)
    assert count_with_knowledge == 2


def test_chinese_terms_find_new_archive_beside_old_index_hits(tmp_path):
    memory_dir = tmp_path / "memory"
    tool_write_memory(memory_dir, "notes", "研究 验收 背景资料：这是旧的合成归档。")
    content = "- 研究资料不足时，应明确标注未知事项及缺失证据，不能把估计当作事实。"
    saved = json.loads(tool_write_memory(memory_dir, "insights", content))
    for query in ["研究", "研究 资料不足", "研究 " + content]:
        hits = json.loads(tool_search_memory(memory_dir, query))["results"]
        assert saved["path"] in {hit["path"] for hit in hits}
        assert hits[0]["path"] == saved["path"]


def test_chinese_keyword_retrieval_keeps_explicit_date_constraint(tmp_path):
    old = json.loads(tool_write_memory(tmp_path, "notes", "2026-10-20 研究资料不足时，应标注未知。"))
    current = json.loads(tool_write_memory(tmp_path, "notes", "2026-10-21 研究资料不足时，应标注未知。"))
    hits = json.loads(tool_search_memory(tmp_path, "2026-10-20 研究 资料不足"))["results"]
    assert old["path"] in {hit["path"] for hit in hits}
    assert current["path"] not in {hit["path"] for hit in hits}
