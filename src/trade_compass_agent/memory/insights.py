"""Proactive trading insight generation — Dreaming Phase 3.

Detects non-obvious signals from observation patterns and decisions:
- Hotness spikes (concept frequency surge)
- Cross-source correlations (pattern ∩ pending decisions)
- Decision behavior patterns (consecutive same-direction trades, win-rate anomalies)
- Risk signals (concentration, losing streaks)
- Missed opportunities (strong patterns without corresponding decisions)
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import date
from enum import Enum
from pathlib import Path

from trade_compass_agent.memory.calendar import record_day
from trade_compass_agent.memory.decision_store import DecisionStore
from trade_compass_agent.memory.observation_store import ObservationStore, validated_concepts
from trade_compass_agent.memory.patterns import TradingPattern

logger = logging.getLogger(__name__)


class InsightKind(str, Enum):
    HOTNESS_SPIKE = "hotness_spike"
    CROSS_SOURCE = "cross_source_pattern"
    DECISION_PATTERN = "decision_pattern"
    RISK_SIGNAL = "risk_signal"
    OPPORTUNITY = "opportunity"


@dataclass
class TradingInsight:
    kind: InsightKind
    title: str
    body: str
    evidence: list[str]
    actionable: str | None
    confidence: float



def _dated_decisions(decisions: list, as_of: date | None, timezone_name: str) -> list:
    if as_of is None:
        return decisions
    return [d for d in decisions if d.decided_at and record_day(d.decided_at, timezone_name) <= as_of
            and (not d.resolved_at or record_day(d.resolved_at, timezone_name) <= as_of)]

# ------------------------------------------------------------------
# Hotness spike detection
# ------------------------------------------------------------------

def _detect_hotness_spikes(
    obs_store: ObservationStore,
    recent_days: int = 3,
    baseline_days: int = 7,
    min_growth: float = 2.0,
    *, as_of: date | None = None, timezone_name: str = "Asia/Shanghai",
) -> list[TradingInsight]:
    kwargs = {"as_of": as_of, "timezone_name": timezone_name} if as_of is not None else {}
    recent = [(obs, validated_concepts(obs)) for obs in obs_store.recent_by_days(recent_days, **kwargs)]
    baseline = [(obs, validated_concepts(obs)) for obs in obs_store.recent_by_days(baseline_days, **kwargs)]
    recent_freq = Counter(concept for _, concepts in recent for concept in concepts)
    baseline_freq = Counter(concept for _, concepts in baseline for concept in concepts)

    insights: list[TradingInsight] = []
    for concept, recent_count in recent_freq.items():
        baseline_count = baseline_freq.get(concept, 0)
        if baseline_count == 0:
            continue
        # Normalize by time window
        recent_rate = recent_count / recent_days
        baseline_rate = baseline_count / baseline_days
        if baseline_rate == 0:
            continue
        growth = recent_rate / baseline_rate
        if growth >= min_growth and recent_count >= 3:
            confidence = min(1.0, growth / 5.0)
            evidence_ids = [obs.id for obs, concepts in recent if concept in concepts and obs.id][:5]
            insights.append(TradingInsight(
                kind=InsightKind.HOTNESS_SPIKE,
                title=f"{concept} 关注度激增 ({growth:.1f}x)",
                body=(f"近 {recent_days} 天出现 {recent_count} 次；近 {baseline_days} 天（含近{recent_days}天）共 {baseline_count} 次，"
                      f"按日均记录次数比较为 {growth:.1f} 倍。仅表示观察记录中的关注频率，不代表市场资金或收益。"),
                evidence=evidence_ids,
                actionable=f"核对 {concept} 的观察来源与记录覆盖，仅作为研究线索",
                confidence=confidence,
            ))
    return sorted(insights, key=lambda x: -x.confidence)[:5]


def _find_obs_ids_for_decisions(
    obs_store: ObservationStore,
    decisions: list,
    *, as_of: date | None = None, timezone_name: str = "Asia/Shanghai",
) -> list[str]:
    """Find observation IDs related to a set of decisions by searching their symbols."""
    seen: set[str] = set()
    result: list[str] = []
    symbols = {d.symbol for d in decisions if d.symbol}
    for symbol in symbols:
        for obs in obs_store.search(symbol, limit=3):
            if as_of is not None and record_day(obs.created_at, timezone_name) > as_of:
                continue
            if obs.id and obs.id not in seen:
                seen.add(obs.id)
                result.append(obs.id)
    return result[:10]


# ------------------------------------------------------------------
# Cross-source pattern correlation
# ------------------------------------------------------------------

def _detect_cross_source(
    patterns: list[TradingPattern],
    dec_store: DecisionStore,
    *, as_of: date | None = None, timezone_name: str = "Asia/Shanghai",
) -> list[TradingInsight]:
    pending = _dated_decisions(dec_store.get_pending(), as_of, timezone_name)
    if not pending or not patterns:
        return []

    pending_symbols = {d.symbol for d in pending}
    insights: list[TradingInsight] = []

    for pattern in patterns:
        overlap = set(pattern.concepts) & pending_symbols
        if overlap:
            insights.append(TradingInsight(
                kind=InsightKind.CROSS_SOURCE,
                title=f"模式「{pattern.theme}」关联待决策标的",
                body=f"发现的模式涉及 {', '.join(overlap)}，这些标的有待处理的交易决策。模式描述：{pattern.description}",
                evidence=pattern.evidence[:10],
                actionable=f"检查 {', '.join(overlap)} 的待决策是否需要根据此模式调整",
                confidence=pattern.strength,
            ))
    return insights[:3]


# ------------------------------------------------------------------
# Decision behavior patterns
# ------------------------------------------------------------------

def _detect_decision_patterns(
    dec_store: DecisionStore,
    obs_store: ObservationStore,
    *, as_of: date | None = None, timezone_name: str = "Asia/Shanghai",
) -> list[TradingInsight]:
    insights: list[TradingInsight] = []
    all_decisions = _dated_decisions(dec_store.search(limit=100), as_of, timezone_name)
    if len(all_decisions) < 3:
        return []

    # Consecutive same-direction trades
    recent = all_decisions[-10:]
    side_counts = Counter(d.side for d in recent)
    dominant_side, dominant_count = side_counts.most_common(1)[0]
    if dominant_count >= 7:
        evidence_ids = _find_obs_ids_for_decisions(obs_store, recent, as_of=as_of, timezone_name=timezone_name)
        insights.append(TradingInsight(
            kind=InsightKind.DECISION_PATTERN,
            title=f"近期交易集中 {dominant_side}",
            body=f"最近 10 笔交易中 {dominant_count} 笔为 {dominant_side}，可能存在方向性偏见",
            evidence=evidence_ids,
            actionable="检查是否存在认知偏差，考虑反向机会",
            confidence=min(1.0, dominant_count / 10),
        ))

    # Win-rate analysis for resolved decisions
    resolved = [d for d in all_decisions if d.outcome_pnl_pct is not None]
    if len(resolved) >= 5:
        wins = [d for d in resolved if d.outcome_pnl_pct > 0]
        win_rate = len(wins) / len(resolved)
        if win_rate < 0.3:
            avg_loss = sum(d.outcome_pnl_pct for d in resolved if d.outcome_pnl_pct <= 0) / max(1, len(resolved) - len(wins))
            evidence_ids = _find_obs_ids_for_decisions(obs_store, resolved[-10:], as_of=as_of, timezone_name=timezone_name)
            insights.append(TradingInsight(
                kind=InsightKind.DECISION_PATTERN,
                title=f"胜率偏低 ({win_rate:.0%})",
                body=f"近 {len(resolved)} 笔已结算交易中，胜率 {win_rate:.0%}，平均亏损 {avg_loss:.1f}%",
                evidence=evidence_ids,
                actionable="建议回顾入场逻辑和止损策略",
                confidence=0.8,
            ))
        elif win_rate > 0.7 and len(resolved) >= 10:
            avg_gain = sum(d.outcome_pnl_pct for d in wins) / len(wins) if wins else 0
            evidence_ids = _find_obs_ids_for_decisions(obs_store, wins[-5:], as_of=as_of, timezone_name=timezone_name)
            insights.append(TradingInsight(
                kind=InsightKind.DECISION_PATTERN,
                title=f"高胜率策略 ({win_rate:.0%})",
                body=f"近 {len(resolved)} 笔交易胜率 {win_rate:.0%}，平均盈利 {avg_gain:.1f}%",
                evidence=evidence_ids,
                actionable="总结当前策略要素，考虑适当加大仓位",
                confidence=0.7,
            ))

    return insights


# ------------------------------------------------------------------
# Risk signals
# ------------------------------------------------------------------

def _detect_risk_signals(
    dec_store: DecisionStore,
    patterns: list[TradingPattern],
    obs_store: ObservationStore,
    *, as_of: date | None = None, timezone_name: str = "Asia/Shanghai",
) -> list[TradingInsight]:
    insights: list[TradingInsight] = []
    pending = _dated_decisions(dec_store.get_pending(), as_of, timezone_name)
    if not pending:
        return []

    # Concentration risk: too many positions in same sector
    symbols = [d.symbol for d in pending]
    symbol_freq = Counter(symbols)
    if len(pending) >= 3:
        most_common_sym, count = symbol_freq.most_common(1)[0]
        if count >= 3:
            concentrated = [d for d in pending if d.symbol == most_common_sym]
            evidence_ids = _find_obs_ids_for_decisions(obs_store, concentrated, as_of=as_of, timezone_name=timezone_name)
            insights.append(TradingInsight(
                kind=InsightKind.RISK_SIGNAL,
                title=f"持仓集中度过高: {most_common_sym}",
                body=f"当前 {len(pending)} 笔待决策中有 {count} 笔涉及 {most_common_sym}",
                evidence=evidence_ids,
                actionable="考虑分散持仓，降低单一标的风险",
                confidence=0.85,
            ))

    # Losing streak
    resolved = dec_store.search(status="resolved", limit=20) + dec_store.search(status="reflected", limit=20)
    resolved = sorted(_dated_decisions(resolved, as_of, timezone_name), key=lambda d: d.resolved_at or d.decided_at)
    if len(resolved) >= 3:
        streak = 0
        for d in reversed(resolved):
            if d.outcome_pnl_pct is not None and d.outcome_pnl_pct < 0:
                streak += 1
            else:
                break
        if streak >= 3:
            streak_decisions = resolved[-streak:]
            evidence_ids = _find_obs_ids_for_decisions(obs_store, streak_decisions, as_of=as_of, timezone_name=timezone_name)
            insights.append(TradingInsight(
                kind=InsightKind.RISK_SIGNAL,
                title=f"连续亏损 {streak} 笔",
                body=f"最近 {streak} 笔交易均为亏损，建议暂停交易，重新评估策略",
                evidence=evidence_ids,
                actionable="暂停开仓，复盘近期失误",
                confidence=min(1.0, streak / 5),
            ))

    return insights


# ------------------------------------------------------------------
# Missed opportunities
# ------------------------------------------------------------------

def _detect_opportunities(
    patterns: list[TradingPattern],
    dec_store: DecisionStore,
    *, as_of: date | None = None, timezone_name: str = "Asia/Shanghai",
) -> list[TradingInsight]:
    if not patterns:
        return []

    all_decisions = _dated_decisions(dec_store.search(limit=200), as_of, timezone_name)
    decision_symbols = {d.symbol for d in all_decisions}

    insights: list[TradingInsight] = []
    for pattern in patterns:
        if pattern.strength < 0.5:
            continue
        untraded = [c for c in pattern.concepts if c not in decision_symbols and len(c) == 6 and c[0] in "036"]
        if untraded:
            insights.append(TradingInsight(
                kind=InsightKind.OPPORTUNITY,
                title=f"模式「{pattern.theme}」中未交易标的",
                body=f"模式强度 {pattern.strength:.0%}，但 {', '.join(untraded)} 从未出现在交易决策中",
                evidence=pattern.evidence[:5],
                actionable=f"评估 {', '.join(untraded)} 是否有交易价值",
                confidence=pattern.strength * 0.7,
            ))
    return insights[:3]


# ------------------------------------------------------------------
# Public API
# ------------------------------------------------------------------

def _load_previous_insights(memory_dir: Path, *, as_of: date | None = None) -> list[dict]:
    """Load the most recent past insights file for dedup."""
    insights_dir = memory_dir / "dreaming" / "insights"
    if not insights_dir.is_dir():
        return []
    today = (as_of or date.today()).isoformat()
    for f in sorted(insights_dir.glob("*.json"), reverse=True):
        if f.stem >= today:
            continue
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, KeyError):
            continue
    return []


def _dedup_insights(
    insights: list[TradingInsight],
    previous: list[dict],
) -> list[TradingInsight]:
    """Remove insights whose kind+title match a previous day's insight."""
    if not previous:
        return insights
    prev_keys = {(item.get("kind", ""), item.get("title", "")) for item in previous}
    return [i for i in insights if (i.kind.value, i.title) not in prev_keys]


def generate_insights(
    obs_store: ObservationStore,
    dec_store: DecisionStore,
    patterns: list[TradingPattern],
    *,
    memory_dir: Path | None = None,
    as_of: date | None = None,
    timezone_name: str = "Asia/Shanghai",
) -> list[TradingInsight]:
    """Generate all trading insights. Returns sorted by confidence desc.

    When memory_dir is provided, deduplicates against previous day's insights.
    """
    all_insights: list[TradingInsight] = []
    all_insights.extend(_detect_hotness_spikes(obs_store, as_of=as_of, timezone_name=timezone_name))
    all_insights.extend(_detect_cross_source(patterns, dec_store, as_of=as_of, timezone_name=timezone_name))
    all_insights.extend(_detect_decision_patterns(dec_store, obs_store, as_of=as_of, timezone_name=timezone_name))
    all_insights.extend(_detect_risk_signals(dec_store, patterns, obs_store, as_of=as_of, timezone_name=timezone_name))
    all_insights.extend(_detect_opportunities(patterns, dec_store, as_of=as_of, timezone_name=timezone_name))

    if memory_dir:
        prev = _load_previous_insights(memory_dir, as_of=as_of)
        if prev:
            all_insights = _dedup_insights(all_insights, prev)

    all_insights.sort(key=lambda x: -x.confidence)
    return all_insights


def persist_insights(memory_dir: Path, insights: list[TradingInsight], *, as_of: date | None = None) -> Path:
    """Save insights to memory_vault/dreaming/insights/{date}.json."""
    from trade_compass_agent.llm.providers import check_execution_active
    check_execution_active()
    today = (as_of or date.today()).isoformat()
    out_dir = memory_dir / "dreaming" / "insights"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{today}.json"
    data = [asdict(i) for i in insights]
    out_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("Persisted %d insights to %s", len(insights), out_path)
    return out_path
