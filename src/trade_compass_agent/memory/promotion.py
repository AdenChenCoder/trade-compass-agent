"""Multi-signal promotion scoring — Dreaming Phase 1.

Replaces the simple importance-threshold filter with a 6-dimensional weighted
score. The ranking combines independent usage, grounding, and outcome signals.

Four-gate promotion pipeline:
  Gate 1: Statistical threshold (existing scoring)
  Gate 2: Concept clustering (group related observations)
  Gate 3: LLM synthesis (extract reusable rule from cluster)
  Gate 4: LLM judgment (KNOWLEDGE / USER / ARCHIVE / REJECT)
"""

from __future__ import annotations

import json
import logging
import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Callable

from trade_compass_agent.memory.contradiction import judge_at_promotion
from trade_compass_agent.memory.memory_store import MemoryStore, _now_iso
from trade_compass_agent.memory.observation_store import Observation, ObservationStore

if TYPE_CHECKING:
    from trade_compass_agent.config import MemoryGovernanceConfig, MemoryPromotionConfig
    from trade_compass_agent.memory.skill_store import SkillStore

logger = logging.getLogger(__name__)

LLM_CALL = Callable[[str, str], str]

# ------------------------------------------------------------------
# Weights & thresholds
# ------------------------------------------------------------------
WEIGHTS = {
    "recall_frequency": 0.25,
    "importance": 0.20,
    "query_diversity": 0.15,
    "recency": 0.15,
    "concept_richness": 0.10,
    "cross_session": 0.15,
}

PROMOTION_THRESHOLD = 0.55
MIN_TOTAL_SIGNAL = 3
MIN_RECALL_DAYS = 1
MAX_PROMOTE_PER_RUN = 5

_RECALL_FREQ_CAP = 8
_DIVERSITY_DAY_CAP = 5
_CONCEPT_CAP = 5
_SESSION_CAP = 3
_RECENCY_HALFLIFE_DAYS = 14


@dataclass
class PromotionCandidate:
    observation: Observation
    score: float
    dimension_scores: dict[str, float]


# ------------------------------------------------------------------
# Dimension scoring
# ------------------------------------------------------------------

def _score_recall_frequency(obs: Observation) -> float:
    return min(1.0, math.log1p(obs.total_signal) / math.log1p(_RECALL_FREQ_CAP))


def _score_importance(obs: Observation) -> float:
    return (obs.importance - 1) / 9.0


def _score_query_diversity(obs: Observation) -> float:
    days = obs.recall_days or []
    return min(1.0, len(days) / _DIVERSITY_DAY_CAP)


def _score_recency(obs: Observation) -> float:
    if not obs.last_recalled_at:
        return 0.0
    try:
        last = datetime.fromisoformat(obs.last_recalled_at)
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        days_ago = max(0.0, (now - last).total_seconds() / 86400)
        return math.exp(-math.log(2) / _RECENCY_HALFLIFE_DAYS * days_ago)
    except (ValueError, TypeError):
        return 0.0


def _score_concept_richness(obs: Observation) -> float:
    return min(1.0, len(obs.concepts) / _CONCEPT_CAP)


def _score_cross_session(obs: Observation) -> float:
    sessions = obs.unique_sessions_recalled or []
    return min(1.0, len(sessions) / _SESSION_CAP)


_SCORERS: dict[str, callable] = {
    "recall_frequency": _score_recall_frequency,
    "importance": _score_importance,
    "query_diversity": _score_query_diversity,
    "recency": _score_recency,
    "concept_richness": _score_concept_richness,
    "cross_session": _score_cross_session,
}


def compute_promotion_score(obs: Observation) -> PromotionCandidate:
    dims: dict[str, float] = {}
    total = 0.0
    for key, scorer in _SCORERS.items():
        val = scorer(obs)
        dims[key] = round(val, 4)
        total += val * WEIGHTS[key]
    return PromotionCandidate(
        observation=obs,
        score=round(total, 4),
        dimension_scores=dims,
    )


# ------------------------------------------------------------------
# Gate + ranking
# ------------------------------------------------------------------

BOOTSTRAP_MIN_SIGNAL = 1
BOOTSTRAP_THRESHOLD = 0.3


def _passes_gate(
    obs: Observation,
    *,
    bootstrap: bool = False,
    skill_store: "SkillStore | None" = None,
) -> bool:
    """Hard gate conditions — must all pass before scoring.

    When *bootstrap* is True, uses relaxed thresholds for cold-start:
    - MIN_TOTAL_SIGNAL → BOOTSTRAP_MIN_SIGNAL (1)
    - Skips consolidated and recall_days checks (impossible to satisfy pre-dreaming)
    """
    if obs.promoted_at is not None:
        return False
    if bootstrap:
        if obs.total_signal < BOOTSTRAP_MIN_SIGNAL:
            return False
    else:
        if obs.total_signal < MIN_TOTAL_SIGNAL:
            return False
        if len(obs.recall_days or []) < MIN_RECALL_DAYS:
            return False
        if not obs.consolidated:
            return False
    from trade_compass_agent.memory.write_gate import quality_check
    ok, _reason = quality_check(obs.summary, skill_store=skill_store)
    if not ok:
        logger.debug("Promotion quality gate rejected %s: %s", obs.id, _reason)
        return False
    return True


def rank_promotion_candidates(
    obs_store: ObservationStore,
    *,
    min_signal: int = MIN_TOTAL_SIGNAL,
    limit: int = 50,
    bootstrap: bool = False,
    skill_store: "SkillStore | None" = None,
) -> list[PromotionCandidate]:
    """Return scored candidates sorted by promotion score (desc).

    Only observations passing the hard gate are scored.
    Uses total_signal (recall + daily + grounded) for candidate selection.

    When *bootstrap* is True, relaxes gate thresholds for cold-start.
    """
    if bootstrap:
        raw = obs_store.promotion_candidates(min_signal=0, limit=limit, require_consolidated=False)
    else:
        raw = obs_store.promotion_candidates(min_signal=min_signal, limit=limit)
    candidates = []
    for obs in raw:
        if not _passes_gate(obs, bootstrap=bootstrap, skill_store=skill_store):
            continue
        candidates.append(compute_promotion_score(obs))
    candidates.sort(key=lambda c: c.score, reverse=True)
    return candidates


@dataclass
class PromotionResult:
    verdict: str  # KNOWLEDGE / USER / SUPERSEDE / ARCHIVE / REJECT / SKIP
    refined_text: str
    reason: str
    source_obs_ids: list[str] = field(default_factory=list)
    conflicts_with: str = ""


# ------------------------------------------------------------------
# LLM Prompts
# ------------------------------------------------------------------

PROMOTION_REFINE_PROMPT = """\
以下是围绕相似概念的多条交易观察记录。
请从中归纳出一条持久有效的交易经验规律（≤80字）。

要求：
- 必须是可复用的规律或教训，不是单次事件记录
- 去除具体日期、价格、工具名，用自然语言表述
- 如果这些观察只是同一事件的重复记录，无法归纳为通用规律，回复 SKIP

观察记录：
{observations}
"""

PROMOTION_JUDGE_PROMPT = """\
你是交易知识审判官。判断以下归纳出的规律是否有资格写入 Agent 的核心知识库。

## 待审内容
{refined_text}

## 来源观察（{count} 条）
{source_observations}

## 审判标准（必须全部满足）
1. 可复用: 不依赖特定标的/日期，可在未来类似场景中指导决策
2. 有验证: 来源观察中有 >= 2 次实战案例支撑
3. 可行动: Agent 读到后明确知道下次遇到该怎么做
4. 不冗余: 不与以下现有知识/技能重复

## 现有 KNOWLEDGE.md
{existing_knowledge}

## 现有 Skills 摘要
{skills_summary}

## 输出格式（JSON）
{{"verdict": "KNOWLEDGE" | "USER" | "ARCHIVE" | "REJECT", "reason": "1-2 句判断理由", "refined": "如果 verdict=KNOWLEDGE，输出精炼后的规律（≤80字）；如果 verdict=USER，输出用户画像条目（≤30字）"}}

- KNOWLEDGE: 满足全部标准，写入核心知识库
- USER: 描述的是用户偏好/风格/约束，写入用户画像
- ARCHIVE: 有价值但不满足全部标准（如只有 1 次案例），归档待积累
- REJECT: 纯事件记录/工具输出/已被覆盖

只返回 JSON，不要其他文字。
"""


# ------------------------------------------------------------------
# Gate 2: Concept clustering
# ------------------------------------------------------------------

def _cluster_by_concepts(
    candidates: list[PromotionCandidate],
) -> list[list[PromotionCandidate]]:
    """Group candidates by overlapping concepts. Min 2 per cluster."""
    concept_to_candidates: dict[str, list[int]] = defaultdict(list)
    for i, c in enumerate(candidates):
        for concept in c.observation.concepts:
            concept_to_candidates[concept].append(i)

    assigned: set[int] = set()
    clusters: list[list[PromotionCandidate]] = []

    for concept in sorted(concept_to_candidates, key=lambda k: -len(concept_to_candidates[k])):
        members = [i for i in concept_to_candidates[concept] if i not in assigned]
        if len(members) < 2:
            continue
        cluster = [candidates[i] for i in members]
        clusters.append(cluster)
        assigned.update(members)

    return clusters


# ------------------------------------------------------------------
# Gate 3 + 4: LLM refine + judge
# ------------------------------------------------------------------

def _llm_refine(
    cluster: list[PromotionCandidate],
    llm_call: LLM_CALL,
) -> str | None:
    """Use LLM to synthesize a reusable rule from a cluster of observations."""
    obs_text = "\n".join(
        f"- {c.observation.summary[:200]}" for c in cluster
    )
    prompt = PROMOTION_REFINE_PROMPT.format(observations=obs_text)
    try:
        result = llm_call("你是一个交易记忆提炼引擎。只返回归纳出的规律文本或 SKIP。", prompt).strip()
        if result.upper() == "SKIP" or not result:
            return None
        return result[:120]
    except Exception as exc:
        logger.warning("LLM refine failed: %s", exc)
        return None


def _llm_judge(
    refined_text: str,
    cluster: list[PromotionCandidate],
    existing_knowledge: str,
    skills_summary: str,
    llm_call: LLM_CALL,
) -> PromotionResult:
    """Use LLM to judge whether the refined rule should enter KNOWLEDGE/USER/ARCHIVE/REJECT."""
    source_text = "\n".join(
        f"- {c.observation.summary[:150]}" for c in cluster
    )
    prompt = PROMOTION_JUDGE_PROMPT.format(
        refined_text=refined_text,
        count=len(cluster),
        source_observations=source_text[:2000],
        existing_knowledge=existing_knowledge[:2000],
        skills_summary=skills_summary[:1000],
    )
    obs_ids = [c.observation.id for c in cluster]

    try:
        raw = llm_call("你是交易知识审判官。只返回合法 JSON。", prompt).strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        data = json.loads(raw)
        verdict = data.get("verdict", "REJECT").upper()
        if verdict not in ("KNOWLEDGE", "USER", "ARCHIVE", "REJECT"):
            verdict = "REJECT"
        final_text = data.get("refined", refined_text) if verdict in ("KNOWLEDGE", "USER") else refined_text
        return PromotionResult(
            verdict=verdict,
            refined_text=final_text[:120],
            reason=data.get("reason", ""),
            source_obs_ids=obs_ids,
        )
    except Exception as exc:
        logger.warning("LLM judge failed: %s", exc)
        return PromotionResult(
            verdict="ARCHIVE",
            refined_text=refined_text,
            reason=f"LLM judge error: {exc}",
            source_obs_ids=obs_ids,
        )


# ------------------------------------------------------------------
# Public API: four-gate promotion
# ------------------------------------------------------------------

def apply_promotions(
    candidates: list[PromotionCandidate],
    mem_store: MemoryStore,
    obs_store: ObservationStore,
    *,
    max_promote: int = MAX_PROMOTE_PER_RUN,
    threshold: float = PROMOTION_THRESHOLD,
    llm_call: LLM_CALL | None = None,
    skill_store: "SkillStore | None" = None,
    governance: "MemoryGovernanceConfig | None" = None,
    promotion_config: "MemoryPromotionConfig | None" = None,
    promoted_by_run_id: str = "",
    promoted_by_job_id: str = "",
) -> list[PromotionResult]:
    """Four-gate promotion pipeline.

    Gate 1: Statistical threshold (pre-filtered by rank_promotion_candidates)
    Gate 2: Concept clustering (group related observations, min 2 per cluster)
    Gate 3: LLM synthesis (refine cluster into a reusable rule)
    Gate 4: LLM judgment (KNOWLEDGE / USER / ARCHIVE / REJECT)

    Falls back to legacy behavior (direct write) when llm_call is None.
    Returns list of PromotionResult with verdict details.
    """
    from trade_compass_agent.config import MemoryGovernanceConfig, MemoryPromotionConfig

    gov = governance or MemoryGovernanceConfig()
    promo_cfg = promotion_config or MemoryPromotionConfig()

    above_threshold = [c for c in candidates if c.score >= threshold]
    if not above_threshold:
        return []

    # Legacy fallback when LLM is unavailable (disabled by default)
    if llm_call is None:
        if gov.legacy_promotion_fallback:
            logger.warning("apply_promotions: llm_call=None, using legacy direct write")
            return _apply_legacy(above_threshold, mem_store, obs_store, max_promote)
        logger.warning("apply_promotions: llm_call=None, legacy disabled — skipping promotion")
        return []

    # Gate 2: Cluster by concepts
    clusters = _cluster_by_concepts(above_threshold)
    if not clusters:
        logger.info("No clusters formed (need >= 2 observations with shared concepts)")
        return []

    existing_active = mem_store.list_active("memory", min_confidence=gov.min_inject_confidence)
    existing_knowledge = "\n".join(m.text for m in existing_active) if existing_active else "（暂无）"
    skills_summary = "（暂无）"
    if skill_store:
        skills = skill_store.list_skills(include_stale=False)
        if skills:
            skills_summary = "\n".join(f"- {s.name}: {s.description or ''}" for s in skills[:20])

    results: list[PromotionResult] = []

    for cluster in clusters:
        if len(results) >= max_promote:
            break

        # Gate 3: LLM refine
        refined = _llm_refine(cluster, llm_call)
        if refined is None:
            logger.debug("Gate 3 SKIP: cluster with %d obs could not be refined", len(cluster))
            continue

        # Gate 4: LLM judge (with GROUNDING + SUPERSEDE)
        result = judge_at_promotion(
            refined,
            cluster,
            existing_knowledge,
            skills_summary,
            llm_call,
            include_grounding=promo_cfg.grounding_in_judge,
        )
        logger.info(
            "Promotion verdict=%s for %d obs: %s (reason: %s)",
            result.verdict, len(cluster), result.refined_text[:50], result.reason,
        )

        promo_meta = {
            "source_obs_ids": result.source_obs_ids,
            "promoted_at": _now_iso(),
            "promoted_by_run_id": promoted_by_run_id,
            "promoted_by_job_id": promoted_by_job_id,
        }
        promo_confidence = promo_cfg.default_confidence

        if result.verdict == "SUPERSEDE" and promo_cfg.auto_supersede and result.conflicts_with:
            replace_result = mem_store.replace(
                result.conflicts_with,
                result.refined_text,
                target="memory",
                source="promotion",
                confidence=promo_confidence,
                meta_extra=promo_meta,
            )
            if replace_result.get("ok"):
                obs_store.mark_promoted(result.source_obs_ids)
                results.append(result)
            else:
                result.verdict = "REJECT"
                result.reason += f"; SUPERSEDE failed: {replace_result.get('error')}"
                results.append(result)
            continue

        if result.verdict == "KNOWLEDGE":
            write_result = mem_store.add(
                result.refined_text,
                target="memory",
                source="promotion",
                confidence=promo_confidence,
                meta_extra=promo_meta,
            )
            if write_result.get("ok"):
                obs_store.mark_promoted(result.source_obs_ids)
                results.append(result)
            else:
                logger.debug("MemoryStore rejected KNOWLEDGE write: %s", write_result.get("error"))
                result.verdict = "REJECT"
                result.reason += f"; MemStore: {write_result.get('error', '')}"
                results.append(result)

        elif result.verdict == "USER":
            write_result = mem_store.add(
                result.refined_text,
                target="user",
                source="promotion",
                confidence=promo_confidence,
                meta_extra=promo_meta,
            )
            if write_result.get("ok"):
                obs_store.mark_promoted(result.source_obs_ids)
                results.append(result)
            else:
                logger.debug("MemoryStore rejected USER write: %s", write_result.get("error"))
                result.verdict = "REJECT"
                result.reason += f"; MemStore: {write_result.get('error', '')}"
                results.append(result)

        elif result.verdict == "ARCHIVE":
            results.append(result)

    return results


def _apply_legacy(
    candidates: list[PromotionCandidate],
    mem_store: MemoryStore,
    obs_store: ObservationStore,
    max_promote: int,
) -> list[PromotionResult]:
    """Legacy direct-write fallback when LLM is unavailable."""
    results: list[PromotionResult] = []
    for c in candidates:
        if len(results) >= max_promote:
            break
        write_result = mem_store.add(c.observation.summary, target="memory", source="promotion", confidence=0.85)
        if write_result.get("ok"):
            obs_store.mark_promoted([c.observation.id])
            results.append(PromotionResult(
                verdict="KNOWLEDGE",
                refined_text=c.observation.summary,
                reason="legacy fallback (no LLM)",
                source_obs_ids=[c.observation.id],
            ))
            logger.info(
                "Legacy promoted observation %s (score=%.3f): %s",
                c.observation.id, c.score, c.observation.summary[:50],
            )
    return results
