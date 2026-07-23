"""Weekly curator — promotes insights from Episodic to Semantic tier.

Run periodically (e.g. weekly cron or on-demand):
1. Reflect on resolved decisions (Decision Journal → add reflection)
2. Scan high-importance observations for promotion candidates
3. Promote recurring patterns to KNOWLEDGE.md via WriteGate

All writes go through SemanticWriteGate — this script does NOT bypass quality control.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from trade_compass_agent.config import TradingCostConfig
from trade_compass_agent.memory.decision_store import DecisionStore, TradeDecision
from trade_compass_agent.memory.memory_store import MemoryStore
from trade_compass_agent.memory.observation_store import ObservationStore
from trade_compass_agent.memory.write_gate import SemanticWriteGate
from trade_compass_agent.memory.skill_store import SkillStore

logger = logging.getLogger(__name__)


_LLM_REFLECTION_SYSTEM = (
    "你是交易复盘助手。根据已结算交易的事实，写 2-4 句中文复盘："
    "1) 结果与原始逻辑是否一致 2) 做对了什么或错在哪里 3) 一条可执行教训。"
    "只基于给定事实，不要编造行情细节。"
)


def curate_decisions(
    data_dir: Path,
    *,
    max_reflect: int = 5,
    llm_call: Any | None = None,
    trading_costs: TradingCostConfig | None = None,
) -> list[str]:
    """Generate reflections for resolved but unreflected decisions.

    Returns list of decision IDs that were reflected.
    """
    from trade_compass_agent.memory.decision_reconciler import reconcile_decisions

    reconcile_decisions(data_dir, trading_costs)
    store = DecisionStore(data_dir)
    resolved = store.search(status="resolved", limit=max_reflect)
    reflected_ids = []

    for d in resolved:
        reflection = generate_decision_reflection(d, llm_call=llm_call)
        if reflection and store.add_reflection(d.id, reflection):
            reflected_ids.append(d.id)
            logger.info("Reflected on decision %s: %s", d.id, reflection[:50])

    return reflected_ids


def generate_decision_reflection(
    d: TradeDecision,
    *,
    llm_call: Any | None = None,
    manual_text: str | None = None,
) -> str:
    """Generate reflection text for a resolved decision."""
    if manual_text and manual_text.strip():
        return manual_text.strip()
    if d.outcome_pnl_pct is None:
        return ""
    if llm_call is not None:
        try:
            text = llm_call(_LLM_REFLECTION_SYSTEM, _reflection_user_prompt(d)).strip()
            if text:
                return text
        except Exception:
            logger.warning("LLM reflection failed for decision %s, falling back to rules", d.id, exc_info=True)
    return _generate_reflection(d)


def _reflection_user_prompt(d: TradeDecision) -> str:
    payload = {
        "symbol": d.symbol,
        "side": d.side,
        "quantity": d.quantity,
        "entry_price": d.price,
        "exit_price": d.outcome_price,
        "pnl_pct": d.outcome_pnl_pct,
        "holding_days": d.holding_days,
        "reasoning": d.reasoning,
        "market_context": d.market_context,
        "source_skills": d.source_skills,
        "decided_at": d.decided_at[:10] if d.decided_at else "",
        "resolved_at": d.resolved_at[:10] if d.resolved_at else "",
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _generate_reflection(d: TradeDecision) -> str:
    """Generate a brief reflection for a resolved decision (rule-based fallback)."""
    if d.outcome_pnl_pct is None:
        return ""

    parts = []
    if d.outcome_pnl_pct > 0:
        parts.append(f"盈利 {d.outcome_pnl_pct:+.1f}%")
        if d.holding_days and d.holding_days <= 3:
            parts.append("短线快进快出有效")
        elif d.holding_days and d.holding_days > 7:
            parts.append("持股耐心获得回报")
    else:
        parts.append(f"亏损 {d.outcome_pnl_pct:.1f}%")
        if d.holding_days and d.holding_days > 5:
            parts.append("未及时止损")
        else:
            parts.append("需重新评估入场时机")

    if d.reasoning:
        parts.append(f"原始逻辑: {d.reasoning}")

    return "；".join(parts)


def promote_observations(
    data_dir: Path,
    memory_dir: Path,
    *,
    max_promote: int = 5,
    min_importance: int = 8,  # kept for signature compat (unused)
) -> list[str]:
    """Promote observations to KNOWLEDGE.md via multi-signal scoring.

    Uses the 6-dimensional scoring system (Dreaming Phase 1) instead of the
    old importance-only filter.  Falls back to importance-based selection
    when no recall data is available yet.

    Returns list of promoted texts.
    """
    from trade_compass_agent.memory.promotion import (
        apply_promotions,
        rank_promotion_candidates,
    )

    obs_store = ObservationStore(data_dir / "observations.db")
    skill_store = SkillStore(memory_dir / "skills")
    gate = SemanticWriteGate(skill_store=skill_store)
    mem_store = MemoryStore(memory_dir, write_gate=gate)

    candidates = rank_promotion_candidates(obs_store, skill_store=skill_store)
    if candidates:
        promoted_candidates = apply_promotions(
            candidates, mem_store, obs_store, max_promote=max_promote,
        )
        return [c.refined_text for c in promoted_candidates if c.verdict in ("KNOWLEDGE", "USER")]

    # Fallback: no recall data yet — use importance threshold
    high = obs_store.high_importance(min_importance=min_importance, limit=20)
    promoted: list[str] = []
    for obs in high:
        if len(promoted) >= max_promote:
            break
        result = mem_store.add(obs.summary, target="memory", source="curator")
        if result.get("ok"):
            obs_store.mark_promoted([obs.id])
            promoted.append(obs.summary)
            logger.info("Promoted observation to KNOWLEDGE (fallback): %s", obs.summary[:50])
    return promoted


def run_weekly_curation(data_dir: Path, memory_dir: Path) -> dict:
    """Run the full weekly curation pipeline."""
    reflected = curate_decisions(data_dir)
    promoted = promote_observations(data_dir, memory_dir)
    return {
        "decisions_reflected": len(reflected),
        "observations_promoted": len(promoted),
        "reflected_ids": reflected,
        "promoted_texts": promoted,
    }
