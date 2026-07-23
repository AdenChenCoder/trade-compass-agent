"""Structural checks at write time and semantic judgment at promotion (Gate 4).

No domain-specific regex rules — grounding context is passed to the LLM judge.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

from trade_compass_agent.memory.write_gate import is_raw_tool_output

if TYPE_CHECKING:
    from trade_compass_agent.memory.memory_store import EntryMeta, MemoryStore
    from trade_compass_agent.memory.promotion import PromotionCandidate, PromotionResult

logger = logging.getLogger(__name__)

LLM_CALL = Callable[[str, str], str]

@dataclass
class ConflictReport:
    """Curator scan result for a conflicting KNOWLEDGE entry."""

    verdict: str  # SUPERSEDE | ARCHIVE | KEEP
    entry_prefix: str
    reason: str
    refined_text: str = ""
    conflicts_with: str = ""


CURATOR_SCAN_PROMPT = """\
你是 KNOWLEDGE 策展人。审查以下活跃知识条目，找出：
1. 与 GROUNDING 硬约束冲突的条目（如 min-lot、数据真实性、交易制度）
2. 互相矛盾或重复的条目（应 SUPERSEDE 修正版，而非并存）
3. 应归档的过时/低价值条目

## GROUNDING 硬约束
{grounding_rules}

## 活跃 KNOWLEDGE 条目（{count} 条）
{entries_block}

## 现有 Skills 摘要
{skills_summary}

返回 JSON 数组。每项格式：
{{"verdict": "SUPERSEDE"|"ARCHIVE"|"KEEP", "entry_prefix": "≥10字前缀定位条目", "reason": "1-2句", "refined": "SUPERSEDE时的新文本≤80字", "conflicts_with": "SUPERSEDE时旧条前缀"}}

- SUPERSEDE: 新条修正旧条，conflicts_with 必填
- ARCHIVE: 软归档，不再注入
- KEEP: 无问题

无问题时返回 []。只返回 JSON 数组。
"""

PROMOTION_JUDGE_WITH_GROUNDING_PROMPT = """\
你是交易知识审判官。判断以下归纳出的规律是否有资格写入 Agent 的核心知识库。

## 待审内容
{refined_text}

## 来源观察（{count} 条）
{source_observations}

## 硬约束（GROUNDING — 高于一切软记忆）
{grounding_rules}

## 审判标准（必须全部满足）
1. 可复用: 不依赖特定标的/日期，可在未来类似场景中指导决策
2. 有验证: 来源观察中有 >= 2 次实战案例支撑
3. 可行动: Agent 读到后明确知道下次遇到该怎么做
4. 不冗余: 不与以下现有知识/技能重复
5. 不矛盾: 与现有 KNOWLEDGE、Skills、GROUNDING 硬约束不冲突；若新条是旧条的修正版 → SUPERSEDE 而非并存

## 现有 KNOWLEDGE.md
{existing_knowledge}

## 现有 Skills 摘要
{skills_summary}

## 输出格式（JSON）
{{"verdict": "KNOWLEDGE" | "USER" | "SUPERSEDE" | "ARCHIVE" | "REJECT", "reason": "1-2 句判断理由", "refined": "精炼文本（≤80字 KNOWLEDGE / ≤30字 USER）", "conflicts_with": "SUPERSEDE 时必填：冲突旧条目的文本前缀（≥10字）"}}

- KNOWLEDGE: 满足全部标准，写入核心知识库
- USER: 描述用户偏好/风格/约束，写入用户画像
- SUPERSEDE: 新条修正旧条，替换而非并存（conflicts_with 必填）
- ARCHIVE: 有价值但不满足全部标准，归档待积累
- REJECT: 纯事件记录/工具输出/与硬约束冲突

只返回 JSON，不要其他文字。
"""


def structural_check(text: str, store: "MemoryStore | None" = None) -> tuple[bool, str]:
    """Layer B: injection / empty / raw tool output only — no domain semantics."""
    cleaned = text.strip()
    if not cleaned:
        return False, "Empty entry"
    if store is not None and store._scan_threats(cleaned):
        return False, "Content blocked by safety filter"
    if is_raw_tool_output(cleaned):
        return False, "Raw tool output rejected"
    return True, ""


def judge_at_promotion(
    refined_text: str,
    cluster: list["PromotionCandidate"],
    existing_knowledge: str,
    skills_summary: str,
    llm_call: LLM_CALL,
    *,
    grounding_rules: str | None = None,
    include_grounding: bool = True,
) -> "PromotionResult":
    """Gate 4: LLM judgment with optional GROUNDING context and SUPERSEDE verdict."""
    from trade_compass_agent.memory.promotion import PromotionResult
    from trade_compass_agent.runtime.bootstrap import GROUNDING_RULES

    source_text = "\n".join(f"- {c.observation.summary[:150]}" for c in cluster)
    obs_ids = [c.observation.id for c in cluster]
    grounding = (grounding_rules or GROUNDING_RULES) if include_grounding else "（未注入硬约束）"

    prompt = PROMOTION_JUDGE_WITH_GROUNDING_PROMPT.format(
        refined_text=refined_text,
        count=len(cluster),
        source_observations=source_text[:2000],
        grounding_rules=grounding[:4000],
        existing_knowledge=existing_knowledge[:2000],
        skills_summary=skills_summary[:1000],
    )

    try:
        raw = llm_call("你是交易知识审判官。只返回合法 JSON。", prompt).strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        data = json.loads(raw)
        verdict = str(data.get("verdict", "REJECT")).upper()
        allowed = ("KNOWLEDGE", "USER", "SUPERSEDE", "ARCHIVE", "REJECT")
        if verdict not in allowed:
            verdict = "REJECT"
        final_text = data.get("refined", refined_text) if verdict in ("KNOWLEDGE", "USER", "SUPERSEDE") else refined_text
        return PromotionResult(
            verdict=verdict,
            refined_text=str(final_text)[:120],
            reason=str(data.get("reason", "")),
            source_obs_ids=obs_ids,
            conflicts_with=str(data.get("conflicts_with", "") or ""),
        )
    except Exception as exc:
        logger.warning("Gate 4 judge failed: %s", exc)
        return PromotionResult(
            verdict="ARCHIVE",
            refined_text=refined_text,
            reason=f"LLM judge error: {exc}",
            source_obs_ids=obs_ids,
        )


def scan_active_conflicts(
    entries: list["EntryMeta"],
    grounding_rules: str,
    skills_summary: str,
    llm_call: LLM_CALL,
) -> list[ConflictReport]:
    """Batch scan active KNOWLEDGE entries for conflicts with grounding or each other."""
    if not entries:
        return []

    numbered = "\n".join(
        f"{i + 1}. [{m.source} conf={m.confidence:.2f}] {m.text[:120]}"
        for i, m in enumerate(entries)
    )
    prompt = CURATOR_SCAN_PROMPT.format(
        grounding_rules=grounding_rules[:4000],
        count=len(entries),
        entries_block=numbered[:4000],
        skills_summary=skills_summary[:1000] or "（暂无）",
    )

    try:
        raw = llm_call("你是 KNOWLEDGE 策展人。只返回合法 JSON 数组。", prompt).strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        data = json.loads(raw)
        if not isinstance(data, list):
            return []
    except Exception as exc:
        logger.warning("Curator scan failed: %s", exc)
        return []

    reports: list[ConflictReport] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        verdict = str(item.get("verdict", "KEEP")).upper()
        if verdict not in ("SUPERSEDE", "ARCHIVE"):
            continue
        prefix = str(item.get("entry_prefix", "") or item.get("conflicts_with", "")).strip()
        if len(prefix) < 3:
            continue
        reports.append(ConflictReport(
            verdict=verdict,
            entry_prefix=prefix,
            reason=str(item.get("reason", "")),
            refined_text=str(item.get("refined", ""))[:120],
            conflicts_with=str(item.get("conflicts_with", prefix))[:120],
        ))
    return reports


def apply_conflict_reports(
    reports: list[ConflictReport],
    mem_store: "MemoryStore",
    *,
    target: str = "memory",
) -> list[dict[str, str]]:
    """Apply curator scan results via replace (SUPERSEDE) or archive_entry (ARCHIVE)."""
    applied: list[dict[str, str]] = []
    for report in reports:
        if report.verdict == "SUPERSEDE" and report.refined_text and report.conflicts_with:
            result = mem_store.replace(
                report.conflicts_with,
                report.refined_text,
                target,
                source="curator",
                confidence=0.85,
            )
            if result.get("ok"):
                applied.append({
                    "action": "SUPERSEDE",
                    "reason": report.reason,
                    "text": report.refined_text[:80],
                })
                logger.info("Curator SUPERSEDE: %s → %s", report.conflicts_with[:30], report.refined_text[:40])
            else:
                logger.warning("Curator SUPERSEDE failed: %s", result.get("error"))
        elif report.verdict == "ARCHIVE":
            result = mem_store.archive_entry(report.entry_prefix, target)
            if result.get("ok"):
                applied.append({
                    "action": "ARCHIVE",
                    "reason": report.reason,
                    "text": report.entry_prefix[:80],
                })
                logger.info("Curator ARCHIVE: %s", report.entry_prefix[:40])
    return applied
