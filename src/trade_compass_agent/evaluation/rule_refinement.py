"""Automatic rule refinement — extract lessons from signal outcomes.

Analyzes closed tracked signals and produces:
- Pattern recognition (what worked / what didn't)
- Rule candidates for memory storage
- Confidence adjustments for future signals

Decision outcomes provide evidence for proposing rule refinements.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from trade_compass_agent.evaluation.signal_tracker import SignalTracker, TrackedSignal

logger = logging.getLogger(__name__)


@dataclass
class RefinementInsight:
    """A lesson extracted from signal outcome analysis."""

    category: str  # win_pattern, loss_pattern, confidence_bias, timing
    description: str
    evidence_count: int
    confidence: float  # how sure we are about this pattern
    suggested_rule: str  # natural language rule for memory


@dataclass
class RefinementReport:
    """Collection of insights from signal outcome analysis."""

    total_analyzed: int
    insights: list[RefinementInsight] = field(default_factory=list)
    confidence_bias: float = 0.0  # positive = overconfident, negative = underconfident
    best_rating: str = ""
    worst_rating: str = ""


def analyze_outcomes(data_dir: Path, min_signals: int = 10) -> RefinementReport:
    """Analyze closed signals and extract refinement insights.

    Only runs when sufficient data exists (>= min_signals closed signals).
    """
    tracker = SignalTracker(data_dir)
    closed = tracker.get_closed(limit=200)

    if len(closed) < min_signals:
        return RefinementReport(total_analyzed=len(closed))

    insights: list[RefinementInsight] = []

    # Confidence bias analysis
    bias = _analyze_confidence_bias(closed)
    if abs(bias) > 0.1:
        direction = "过于自信" if bias > 0 else "过于保守"
        insights.append(RefinementInsight(
            category="confidence_bias",
            description=f"信号置信度{direction}: 偏差{bias:.2f}",
            evidence_count=len(closed),
            confidence=min(len(closed) / 30, 1.0),
            suggested_rule=f"置信度校准: 建议将原始置信度{'降低' if bias > 0 else '提高'}{abs(bias)*100:.0f}%",
        ))

    # Rating effectiveness
    rating_stats = _analyze_by_rating(closed)
    best = max(rating_stats.items(), key=lambda x: x[1], default=("", 0))
    worst = min(rating_stats.items(), key=lambda x: x[1], default=("", 0))

    if best[0] and best[1] > 0.6:
        insights.append(RefinementInsight(
            category="win_pattern",
            description=f"'{best[0]}' 评级准确率最高: {best[1]:.0%}",
            evidence_count=len([s for s in closed if s.rating == best[0]]),
            confidence=0.7,
            suggested_rule=f"优先信任 '{best[0]}' 评级的信号",
        ))

    if worst[0] and worst[1] < 0.3:
        insights.append(RefinementInsight(
            category="loss_pattern",
            description=f"'{worst[0]}' 评级失败率高: 胜率仅{worst[1]:.0%}",
            evidence_count=len([s for s in closed if s.rating == worst[0]]),
            confidence=0.7,
            suggested_rule=f"对 '{worst[0]}' 评级信号提高警惕，降低仓位",
        ))

    # Stop-loss adherence
    stop_insights = _analyze_stop_loss(closed)
    if stop_insights:
        insights.append(stop_insights)

    return RefinementReport(
        total_analyzed=len(closed),
        insights=insights,
        confidence_bias=bias,
        best_rating=best[0] if best[0] else "",
        worst_rating=worst[0] if worst[0] else "",
    )


def _analyze_confidence_bias(signals: list[TrackedSignal]) -> float:
    """Compute confidence bias: difference between stated confidence and actual win rate."""
    if not signals:
        return 0.0
    avg_confidence = sum(s.confidence for s in signals) / len(signals)
    wins = sum(1 for s in signals if s.outcome == "win")
    actual_rate = wins / len(signals)
    return avg_confidence - actual_rate


def _analyze_by_rating(signals: list[TrackedSignal]) -> dict[str, float]:
    """Compute win rate per rating category."""
    from collections import defaultdict
    counts: dict[str, list[bool]] = defaultdict(list)
    for s in signals:
        if s.rating:
            counts[s.rating].append(s.outcome == "win")
    return {
        rating: sum(wins) / len(wins) if wins else 0.0
        for rating, wins in counts.items()
        if len(wins) >= 3
    }


def _analyze_stop_loss(signals: list[TrackedSignal]) -> RefinementInsight | None:
    """Analyze stop-loss behavior."""
    with_stop = [s for s in signals if s.stop_loss is not None and s.actual_entry is not None]
    if len(with_stop) < 5:
        return None

    losses = [s for s in with_stop if s.outcome == "loss"]
    if not losses:
        return None

    breached_stop = 0
    for s in losses:
        if s.actual_exit and s.stop_loss and s.actual_exit < s.stop_loss:
            breached_stop += 1

    if breached_stop > len(losses) * 0.5:
        return RefinementInsight(
            category="timing",
            description=f"止损未严格执行: {breached_stop}/{len(losses)}笔亏损交易出场价低于止损价",
            evidence_count=breached_stop,
            confidence=0.8,
            suggested_rule="严格在止损价执行卖出，不应犹豫或期望反弹",
        )
    return None
