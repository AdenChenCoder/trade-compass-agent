"""Agent tools for self-improvement: write_knowledge + skill_manage."""

from __future__ import annotations

import json
import re

from trade_compass_agent.config import MemoryGovernanceConfig
from trade_compass_agent.memory.contradiction import structural_check
from trade_compass_agent.memory.memory_store import MemoryStore
from trade_compass_agent.memory.skill_store import SkillStore
from trade_compass_agent.memory.write_gate import quality_check

_AGENT_ACTORS = frozenset({"agent", "background_review", "scheduler", "dreaming"})
_USER_ONLY_ACTIONS = frozenset({"pin", "forget"})
_PROCEDURAL_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"\bload_skill\s*\(", "contains load_skill routing"),
    (r"\b(?:get_bars|compute_ma|compute_rsi|compute_macd|compute_bollinger|compute_volume_ratio|chart_pattern|get_fund_flow|analyze_portfolio|map_intent_to_sell|emit_signal)\s*\(", "contains tool call sequence"),
)


def _resolve_write_source(actor: str, source: str) -> str:
    if actor == "background_review":
        return "background_review"
    if actor == "scheduler":
        return "scheduler"
    if actor == "dreaming":
        return "dreaming"
    if actor == "curator":
        return "curator"
    if actor == "user":
        return "user"
    return "agent"


def _skill_store_from_memory_store(store: MemoryStore) -> SkillStore | None:
    gate = getattr(store, "_write_gate", None)
    return getattr(gate, "skill_store", None)


def _procedural_rejection_reason(content: str) -> str:
    text = content.strip()
    if not text:
        return ""
    for pattern, reason in _PROCEDURAL_PATTERNS:
        if re.search(pattern, text, flags=re.IGNORECASE):
            return reason

    ordered_steps = re.findall(r"(?m)^\s*\d+[.)、]\s+\S+", text)
    if len(ordered_steps) >= 2:
        return "contains ordered execution steps"

    table_rows = re.findall(r"(?m)^\s*\|.+\|\s*$", text)
    if len(table_rows) >= 3:
        return "contains tabular procedure or rubric"

    tool_mentions = re.findall(
        r"\b(?:get_bars|compute_ma|compute_rsi|compute_macd|compute_bollinger|compute_volume_ratio|chart_pattern|get_fund_flow|analyze_portfolio|map_intent_to_sell|emit_signal)\b",
        text,
    )
    if len(set(tool_mentions)) >= 2:
        return "contains multiple tool names"
    return ""


def tool_memory_write(
    store: MemoryStore,
    action: str,
    content: str = "",
    target: str = "memory",
    old_text: str = "",
    source: str = "agent",
    actor: str = "agent",
    governance: MemoryGovernanceConfig | None = None,
) -> str:
    """Manage bounded declarative memory with trust-tiered writes."""
    gov = governance or MemoryGovernanceConfig()

    if action == "list":
        entries = store.list_active(target, min_confidence=0.0)
        items = [
            {
                "text": m.text,
                "confidence": round(m.confidence, 3),
                "source": m.source,
                "status": m.status,
                "content_hash": m.content_hash or m.dedup_hash,
            }
            for m in entries
        ]
        return json.dumps(
            {"target": target, "entries": items, "count": len(items), "min_inject": gov.min_inject_confidence},
            ensure_ascii=False,
        )

    if action in ("replace", "remove") and actor in _AGENT_ACTORS:
        return json.dumps(
            {"ok": False, "error": "Agent cannot replace or remove KNOWLEDGE entries; use pin/forget (user) or promotion."},
            ensure_ascii=False,
        )

    if action in _USER_ONLY_ACTIONS and actor != "user":
        return json.dumps(
            {"ok": False, "error": f"Only user actor can {action}"},
            ensure_ascii=False,
        )

    if action == "pin":
        ok, err = structural_check(content, store)
        if not ok:
            return json.dumps({"ok": False, "error": err}, ensure_ascii=False)
        result = store.add(
            content,
            target=target,
            source="user_pin",
            confidence=1.0,
            allow_supersede=True,
            allow_reinforce=True,
        )
        return json.dumps(result, ensure_ascii=False)

    if action == "forget":
        if not content.strip():
            return json.dumps({"ok": False, "error": "content required (text prefix to forget)"}, ensure_ascii=False)
        result = store.archive_entry(content, target=target)
        return json.dumps(result, ensure_ascii=False)

    if action == "add":
        if target == "memory" and actor in _AGENT_ACTORS:
            procedural_reason = _procedural_rejection_reason(content)
            if procedural_reason:
                return json.dumps(
                    {
                        "ok": False,
                        "error": f"Knowledge rejected: procedural content ({procedural_reason}). Use skill_manage(action=patch/create) instead.",
                        "suggested_target": "skill",
                        "reason": procedural_reason,
                    },
                    ensure_ascii=False,
                )
            ok, reason = quality_check(content, skill_store=_skill_store_from_memory_store(store))
            if not ok and "skill" in reason.lower():
                return json.dumps(
                    {
                        "ok": False,
                        "error": f"Knowledge rejected: {reason}. Use skill_manage(action=patch/create) instead.",
                        "suggested_target": "skill",
                        "reason": reason,
                    },
                    ensure_ascii=False,
                )
        ok, err = structural_check(content, store)
        if not ok:
            return json.dumps({"ok": False, "error": err}, ensure_ascii=False)
        write_source = _resolve_write_source(actor, source)
        confidence = gov.agent_add_confidence if actor in _AGENT_ACTORS else 1.0
        trusted_actor = actor in {"user", "curator"}
        result = store.add(
            content,
            target=target,
            source=write_source,
            confidence=confidence,
            allow_supersede=trusted_actor,
            allow_reinforce=trusted_actor,
        )
        return json.dumps(result, ensure_ascii=False)

    if action == "replace":
        result = store.replace(old_text, content, target=target)
    elif action == "remove":
        result = store.remove(content, target=target)
    else:
        result = {"ok": False, "error": f"Unknown action: {action}. Use add/replace/remove/list/pin/forget."}
    return json.dumps(result, ensure_ascii=False)


def tool_skill_manage(
    store: SkillStore,
    action: str,
    name: str = "",
    content: str = "",
    old_text: str = "",
    new_text: str = "",
    actor: str = "agent",
) -> str:
    """Manage procedural skills (trading playbooks/strategies)."""
    if action == "list":
        skills = store.list_skills(include_stale=True)
        items = [{"name": s.name, "description": s.description, "category": s.category,
                  "state": s.usage.state, "quality": s.quality.quality,
                  "static_status": s.quality.static_status, "use_count": s.usage.use_count} for s in skills]
        return json.dumps({"skills": items, "count": len(items)}, ensure_ascii=False)

    elif action == "view":
        content_text = store.read_full(name, record_view=True, with_quality_header=True)
        if content_text is None:
            return json.dumps({"ok": False, "error": f"Skill '{name}' not found"}, ensure_ascii=False)
        return json.dumps({"name": name, "content": content_text}, ensure_ascii=False)

    elif action == "create":
        result = store.create(name, content, created_by=_resolve_write_source(actor, "agent"))

    elif action == "patch":
        result = store.patch(name, old_text, new_text)

    elif action == "edit":
        result = store.edit(name, content)

    elif action == "archive":
        result = store.archive(name)

    elif action == "restore":
        result = store.restore(name)

    elif action == "pin":
        result = store.pin(name)

    elif action == "unpin":
        result = store.unpin(name)

    else:
        result = {"ok": False, "error": f"Unknown action: {action}"}

    return json.dumps(result, ensure_ascii=False)


MEMORY_WRITE_SCHEMA = {
    "name": "write_knowledge",
    "description": (
        "管理核心知识库和用户画像。"
        "target='memory' → KNOWLEDGE.md: 声明性记忆，只保存长期判断原则/事实/用户偏好（≤80字/条）。"
        "禁止写入流程、策略/playbook、触发条件、工具调用顺序、评分表、阈值表、输出模板、load_skill 路由。"
        "这些过程性内容必须用 skill_manage(create/patch/edit)。"
        "Agent add 为低信任暂存，默认不注入后续 prompt。"
        "target='user' → USER.md: 用户画像。"
        "pin/forget 仅用户侧操作（action=pin|forget）。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["add", "replace", "remove", "list", "pin", "forget"]},
            "content": {"type": "string", "description": "要添加/替换/遗忘匹配的内容（add≤80字；forget 为条目文本前缀）"},
            "target": {
                "type": "string",
                "enum": ["memory", "user"],
                "default": "memory",
            },
            "old_text": {"type": "string", "description": "replace 时要被替换的原文片段"},
        },
        "required": ["action"],
    },
}

SKILL_MANAGE_SCHEMA = {
    "name": "skill_manage",
    "description": (
        "管理过程性交易技能/策略/playbook。凡是包含触发条件、执行步骤、工具调用、评分/阈值表、输出模板、"
        "或 load_skill 路由的内容，都应使用本工具而不是 write_knowledge。"
        "list=列出, view=查看, create=创建, patch=修补, edit=重写, archive=归档。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["list", "view", "create", "patch", "edit", "archive", "restore", "pin", "unpin"]},
            "name": {"type": "string", "description": "技能名称 (lowercase, a-z0-9._-)"},
            "content": {"type": "string", "description": "create/edit 的完整内容"},
            "old_text": {"type": "string", "description": "patch 时要替换的原文"},
            "new_text": {"type": "string", "description": "patch 时的新文本"},
        },
        "required": ["action"],
    },
}
