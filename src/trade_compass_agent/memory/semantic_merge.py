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

from trade_compass_agent.memory.memory_store import ENTRY_DELIMITER, MemoryStore
from trade_compass_agent.memory.write_gate import jaccard_similarity

logger = logging.getLogger(__name__)

CLUSTER_THRESHOLD = 0.35
MERGE_COOLDOWN_KEY = "last_merge_at"
MERGE_COOLDOWN_HOURS = 24


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
    report: list[dict] | None = None,
) -> int:
    """Scan KNOWLEDGE.md entries, cluster similar ones, merge via LLM.

    Returns number of clusters merged.
    """
    if llm_call is None:
        logger.debug("Semantic merge skipped: no LLM available")
        return 0

    capacity = mem_store.capacity()
    active_metas = [m for m in mem_store.list_active() if m.source != "user_pin" and mem_store.is_trusted_source(m.source)]
    if len(active_metas) < 2:
        return 0
    if not force and not capacity["pressure"] and not _should_run({MERGE_COOLDOWN_KEY: mem_store.maintenance_marker(MERGE_COOLDOWN_KEY)}):
        return 0
    merged = 0
    outcomes = report if report is not None else []
    for cluster in _find_clusters([m.text for m in active_metas]):
        originals = [m for m in active_metas if m.text in cluster]
        try:
            raw = llm_call("你是记忆整理助手。只返回 JSON。", (
                "仅合并同义或互补知识。保留全部条件、范围、例外和时序；矛盾或无法节省空间就返回 {}。"
                "不要仅凭日期判断正确性。返回 {\"content\":\"完整合并文本\",\"reason\":\"保真依据\"}。\n"
                + "\n".join(cluster)))
            proposal = _parse_json(raw)
            content = proposal.get("content", "")
            if not content or len(content) >= len(ENTRY_DELIMITER.join(cluster)):
                continue
            result = evaluate_revision(mem_store, replacements=[{"entry_id": m.entry_id, "version": m.version} for m in originals],
                content=content, reason=proposal.get("reason", "semantic merge"),
                evidence=[f"memory:{m.entry_id}:{m.version}" for m in originals], llm_call=llm_call, change_kind="merged")
            outcomes.append(result)
            merged += int(result.get("ok", False) and result.get("changed", False))
        except Exception as exc:
            logger.warning("Memory merge deferred: %s", exc)
            outcomes.append({"ok": False, "disposition": "evaluation_failed", "error": str(exc)})
    if not any(not r.get("ok") and r.get("disposition") != "pending" for r in outcomes):
        mem_store.maintenance_marker(MERGE_COOLDOWN_KEY, datetime.now(timezone.utc).isoformat())
    return merged


def _parse_json(raw):
    import json
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0]
    value = json.loads(text)
    return value if isinstance(value, dict) else {}


def evaluate_revision(store, *, replacements, content, reason, evidence, llm_call, target="memory", actor="curator",
                      change_kind="replaced"):
    """Judge a proposal outside the storage lock; commit only the version judged.

    This reuses the existing curator role. A model's self-written evidence list
    cannot promote an unverified draft: the inputs must contain admitted records
    or candidates already reviewed by the promotion pipeline.
    """
    from trade_compass_agent.memory.contradiction import structural_check
    from trade_compass_agent.runtime.bootstrap import GROUNDING_RULES
    import json

    valid, error = structural_check(content, store)
    if not valid:
        return {"ok": False, "error": error}
    if not replacements or not reason.strip() or not evidence:
        return {"ok": False, "error": "Revision needs entry IDs/versions, reason and evidence"}
    version = store.revision
    rows = {m.entry_id: m for m in store.get_entries_with_meta(target)}
    originals = []
    for ref in replacements:
        row = rows.get(ref.get("entry_id"))
        if row is None or row.version != ref.get("version"):
            return {"ok": False, "disposition": "version_conflict", "error": "Read current entries and retry"}
        if row.source == "user_pin" and actor != "user":
            return {"ok": False, "disposition": "protected", "error": "Pinned memory is protected"}
        if row.status == "archived" or not store.is_trusted_source(row.source):
            return {"ok": False, "disposition": "pending", "error": "Unverified candidates require evidence-based promotion first"}
        originals.append(row)
    if llm_call is None:
        return {"ok": False, "error": "Semantic evaluation unavailable; original memories preserved"}
    prompt = json.dumps({"originals": [{"id": m.entry_id, "text": m.text, "status": m.status, "evidence": m.source_obs_ids} for m in originals],
        "proposal": content, "reason": reason, "references": evidence,
        "other_core": [m.text for m in store.list_active(target) if m.entry_id not in {o.entry_id for o in originals}]}, ensure_ascii=False)
    try:
        verdict = _parse_json(llm_call("审查记忆修订。保持条件、例外、范围、时序和独有信息；引用自述不算独立验证。"
            "允许有依据的纠错和以更高价值知识取代较低价值内容，但不得仅凭新、长、高频判优。"
            "合并同义知识不要求新增市场证据；新增主张须有输入中可验证依据。"
            "容量退选与证伪必须区分。违反用户规则或无法证明更好则保留原集合。"
            "返回 JSON {valid:boolean, reason:string}。\n" + GROUNDING_RULES + "\n" + _user_rules(store), prompt))
    except Exception as exc:
        return {"ok": False, "disposition": "evaluation_failed", "error": str(exc)}
    if verdict.get("valid") is not True:
        return {"ok": False, "disposition": "pending", "error": verdict.get("reason", "Proposal not accepted")}
    return store.commit_revision(replacements=replacements, content=content, reason=reason,
        evidence=evidence + ["curator: " + str(verdict.get("reason", "validated"))], target=target,
        expected_revision=version, actor=actor, review_method="ai", change_kind=change_kind)


def _user_rules(store):
    from trade_compass_agent.memory.rules_store import RulesStore
    rules = RulesStore(store._memory_dir)
    return rules.read_for_prompt()


def maintain_memory(store, llm_call, *, force=False):
    """Pressure review: merge first, then explicitly compare admission/replacement.

    A content/state fingerprint avoids repeating unchanged evaluations. Reads and
    access counts do not invalidate it. Failed evaluations remain retryable.
    """
    import hashlib
    import json
    rows = store.get_entries_with_meta()
    eligible = [m for m in rows if m.status != "archived" and store.is_trusted_source(m.source)]
    fingerprint = hashlib.sha256(json.dumps([(m.entry_id, m.version, m.status) for m in eligible]).encode()).hexdigest()
    if not force and store.maintenance_marker("reviewed_fingerprint") == fingerprint:
        return {"ok": True, "changed": False, "disposition": "unchanged"}
    if not store.capacity()["maintenance_needed"] and not force:
        return {"ok": True, "changed": False}
    report = []
    merged = merge_similar_entries(store, llm_call, force=force, report=report)
    failures = [r.get("error", "evaluation failed") for r in report if not r.get("ok") and r.get("disposition") != "pending"]
    commits = [r for r in report if r.get("ok") and r.get("changed")]
    if failures:
        return {"ok": False, "changed": bool(commits), "commits": commits,
                "merged_clusters": merged, "error": "; ".join(failures)}
    eligible = [m for m in store.get_entries_with_meta() if m.status != "archived" and store.is_trusted_source(m.source)]
    candidates = [m for m in eligible if m.status == "candidate"]
    changed = bool(merged)
    if candidates:
        try:
            prompt = json.dumps({"capacity": store.capacity(), "entries": [
                {"entry_id": m.entry_id, "version": m.version, "text": m.text, "status": m.status, "pinned": m.source == "user_pin"}
                for m in eligible if m.status == "active" or m in candidates[:20]]}, ensure_ascii=False)
            proposal = _parse_json(llm_call("你是核心记忆策展人，最多选择一次有依据的准入/替换。先去重、保真合并、纠错，再比较新旧价值。"
                "固定不可动；有效集合不得超过额度。全部有价值时仅当新候选明显更好才容量退选，否则返回 {}。"
                "返回 JSON {replacements:[{entry_id,version}],content,reason,evidence:[来源标识]}；replacements包括被采纳的候选和被替换的旧项。", prompt))
            if proposal.get("content"):
                result = evaluate_revision(store, replacements=proposal.get("replacements", []), content=proposal["content"],
                    reason=proposal.get("reason", ""), evidence=proposal.get("evidence", []), llm_call=llm_call)
                if not result.get("ok"):
                    return {**result, "changed": changed, "commits": commits,
                            "merged_clusters": merged}
                changed |= result.get("changed", False)
                if result.get("changed"):
                    commits.append(result)
        except Exception as exc:
            return {"ok": False, "error": str(exc), "changed": changed,
                    "commits": commits, "merged_clusters": merged}
    store.maintenance_marker("reviewed_fingerprint", fingerprint)
    return {"ok": True, "changed": changed, "merged_clusters": merged, "commits": commits}
