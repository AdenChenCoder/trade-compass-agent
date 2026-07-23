"""Semantic merge pass for KNOWLEDGE.md — Dreaming integration.

Scans existing entries, clusters similar ones via Jaccard, and merges
clusters using LLM. Requires LLM; skips silently without one.

Merge rules:
- concept-indexed pairwise Jaccard similarity
- merge into existing entries instead of creating near-duplicates
- append and re-summarize time-adjacent nodes
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Callable

from trade_compass_agent.memory.memory_store import MemoryStore
from trade_compass_agent.memory.write_gate import jaccard_similarity

logger = logging.getLogger(__name__)

CLUSTER_THRESHOLD = 0.35
MIN_ENTRIES_TO_MERGE = 5
MERGE_COOLDOWN_KEY = "last_merge_at"
MERGE_COOLDOWN_HOURS = 24

MERGE_PROMPT = """\
以下是 KNOWLEDGE.md 中语义相近的多条记忆：

{entries}

请合并为一条精炼的知识条目（≤80字），保留核心事实，去除重复表述。
如果各条目存在矛盾，保留最新/最准确的信息。
只输出合并后的一条文本，不要解释。"""


def _find_clusters(entries: list[str], threshold: float = CLUSTER_THRESHOLD) -> list[list[str]]:
    """Union-Find clustering of entries by Jaccard similarity."""
    n = len(entries)
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for i in range(n):
        for j in range(i + 1, n):
            if jaccard_similarity(entries[i], entries[j]) > threshold:
                union(i, j)

    groups: dict[int, list[str]] = {}
    for i in range(n):
        root = find(i)
        groups.setdefault(root, []).append(entries[i])
    return [g for g in groups.values() if len(g) >= 2]


def _should_run(meta: dict[str, Any]) -> bool:
    """Check cooldown: at least MERGE_COOLDOWN_HOURS since last merge."""
    last_str = meta.get(MERGE_COOLDOWN_KEY)
    if not last_str:
        return True
    try:
        last = datetime.fromisoformat(last_str)
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        hours = (datetime.now(timezone.utc) - last).total_seconds() / 3600
        return hours >= MERGE_COOLDOWN_HOURS
    except (ValueError, TypeError):
        return True


def merge_similar_entries(
    mem_store: MemoryStore,
    llm_call: Callable[[str, str], str] | None = None,
    *,
    force: bool = False,
) -> int:
    """Scan KNOWLEDGE.md entries, cluster similar ones, merge via LLM.

    Returns number of clusters merged.
    """
    if llm_call is None:
        logger.debug("Semantic merge skipped: no LLM available")
        return 0

    active_metas = [
        m for m in mem_store.list_active("memory", min_confidence=mem_store.min_inject_confidence)
        if mem_store.is_trusted_source(m.source)
    ]
    entries = [m.text for m in active_metas]
    if len(entries) < MIN_ENTRIES_TO_MERGE:
        return 0

    if not force and not _should_run(mem_store._meta):
        logger.debug("Semantic merge skipped: cooldown not elapsed")
        return 0

    clusters = _find_clusters(entries)
    if not clusters:
        logger.debug("Semantic merge: no similar clusters found")
        mem_store._meta[MERGE_COOLDOWN_KEY] = datetime.now(timezone.utc).isoformat()
        mem_store._save_meta()
        return 0

    merged = 0
    for cluster in clusters:
        numbered = "\n".join(f"{i+1}. {e}" for i, e in enumerate(cluster))
        prompt = MERGE_PROMPT.format(entries=numbered)
        try:
            new_text = llm_call(
                "你是记忆整理助手。只输出合并后的一条文本。",
                prompt,
            ).strip()
        except Exception as exc:
            logger.warning("LLM merge failed for cluster: %s", exc)
            continue

        if not new_text or len(new_text) < 5:
            continue

        # Collect metadata from entries being merged before removal
        max_access = 0
        earliest_created = None
        for m in active_metas:
            if m.text in cluster:
                max_access = max(max_access, m.access_count)
                if earliest_created is None or m.created_at < earliest_created:
                    earliest_created = m.created_at

        for old_entry in cluster:
            mem_store.remove(old_entry[:50], "memory")
        result = mem_store.add(
            new_text,
            target="memory",
            source="curator",
            confidence=0.85,
        )
        if result.get("ok") or result.get("superseded"):
            merged += 1
            # Inherit accumulated metadata from merged entries
            if max_access > 0 or earliest_created:
                metas = mem_store._meta.get("memory", [])
                for m in metas:
                    if m.get("text", "").strip() == new_text.strip():
                        m["access_count"] = max(m.get("access_count", 0), max_access)
                        if earliest_created:
                            m["created_at"] = earliest_created
                        break
                mem_store._save_meta()
            logger.info(
                "Merged %d entries into: %s", len(cluster), new_text[:60],
            )
        else:
            logger.warning("Failed to add merged entry: %s", result)

    mem_store._meta[MERGE_COOLDOWN_KEY] = datetime.now(timezone.utc).isoformat()
    mem_store._save_meta()

    if merged:
        logger.info("Semantic merge: merged %d cluster(s)", merged)
    return merged
