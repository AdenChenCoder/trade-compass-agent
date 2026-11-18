"""Step 2: recall sanitization tests."""
from __future__ import annotations

import json
from pathlib import Path

from trade_compass_agent.memory.tree.search import MemorySearchIndex, search_memory_chunks
from trade_compass_agent.ops.reflection import JobReflection, ResolvedReflection
from trade_compass_agent.runtime.bootstrap import build_system_prompt, MEMORY_AUTHORITY_FOOTER
from trade_compass_agent.memory.memory_store import MemoryStore


def test_search_excludes_knowledge_scope(tmp_path: Path) -> None:
    memory_dir = tmp_path / "vault"
    memory_dir.mkdir()
    index = MemorySearchIndex(memory_dir / "tree" / "chunks.db")
    index.index_file("knowledge", "secret knowledge entry", memory_dir / "KNOWLEDGE.md")
    index.index_file("insights", "tree note about MA20", memory_dir / "tree" / "insights" / "n.md")

    hits = search_memory_chunks(memory_dir, "MA20")
    assert hits
    assert all(h.scope != "knowledge" for h in hits)


def test_reflection_get_context_sanitized(tmp_path: Path) -> None:
    jr = JobReflection(tmp_path / "vault")
    jr._job_dir("premarket")
    resolved = ResolvedReflection(
        job_id="premarket",
        run_id="r1",
        run_date="2026-06-16",
        lesson="考虑止盈",
        resolved_at="2026-06-16T12:00:00",
    )
    path = jr._job_dir("premarket") / "resolved.jsonl"
    path.write_text(json.dumps({
        "job_id": resolved.job_id,
        "run_id": resolved.run_id,
        "run_date": resolved.run_date,
        "predictions": {},
        "actuals": {},
        "lesson": resolved.lesson,
        "resolved_at": resolved.resolved_at,
    }) + "\n", encoding="utf-8")

    ctx = jr.get_context("premarket", sanitize=True)
    assert "[历史-待验证]" in ctx


def test_bootstrap_includes_authority_footer(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "vault", min_inject_confidence=0.5)
    store.add("高信任规律", source="promotion", confidence=0.85)
    store.load_from_disk()
    prompt = build_system_prompt(memory_dir=tmp_path / "vault", skills=[], memory_store=store)
    assert MEMORY_AUTHORITY_FOOTER.strip() in prompt


def test_oversize_memory_is_explicitly_incomplete_on_index_and_fallback(tmp_path, monkeypatch):
    from trade_compass_agent.runtime.tools.memory import tool_search_memory, tool_write_memory
    content = "002415 " + "资料" * 4500 + "尚未执行。"
    tool_write_memory(tmp_path, "notes", content)
    for fallback in (False, True):
        if fallback:
            monkeypatch.setattr(MemorySearchIndex, "search", lambda *a, **k: [])
        hit = json.loads(tool_search_memory(tmp_path, "002415"))["results"][0]
        assert hit["truncated"] is True and hit["content_chars"] > 8000
        assert len(hit["content"]) == 8000


def test_oversize_recent_archive_does_not_hide_fitting_original_context(tmp_path):
    from trade_compass_agent.memory.tree.storage import MemoryTreeStore
    from trade_compass_agent.runtime.tools.memory import bootstrap_memory_context
    store = MemoryTreeStore(tmp_path)
    original = "观察002415，但目前尚未成交。"
    store.write("notes", original)
    store.write("compaction", "历史记忆摘录。" * 400)
    context = bootstrap_memory_context(tmp_path)
    assert original in context and len(context) < 2000
