"""L3 sector/concept resonance — bonus scoring for sector alignment."""

from __future__ import annotations

import logging

from trade_compass_agent.screening.config import ScreeningConfig
from trade_compass_agent.screening.factors import FactorScores

logger = logging.getLogger(__name__)


def apply_resonance_bonus(
    scores: list[FactorScores],
    hot_industries: list[str] | None = None,
    hot_concepts: list[str] | None = None,
    industry_map: dict[str, str] | None = None,
    concept_map: dict[str, list[str]] | None = None,
    cfg: ScreeningConfig | None = None,
) -> list[FactorScores]:
    """Add bonus to stocks aligned with hot sectors/concepts.

    This is a score adjustment, not a filter. Stocks without resonance
    keep their original score unchanged.
    """
    if cfg is None:
        from trade_compass_agent.screening.config import ScreeningConfig
        cfg = ScreeningConfig()

    if not hot_industries and not hot_concepts:
        return scores

    hot_ind_set = set(hot_industries or [])
    hot_con_set = set(hot_concepts or [])

    adjusted: list[FactorScores] = []
    bonus_count = 0

    for s in scores:
        bonus = 0.0

        if industry_map and s.symbol in industry_map:
            if industry_map[s.symbol] in hot_ind_set:
                bonus += cfg.resonance_bonus * 0.6

        if concept_map and s.symbol in concept_map:
            stock_concepts = set(concept_map[s.symbol])
            overlap = stock_concepts & hot_con_set
            if overlap:
                bonus += cfg.resonance_bonus * 0.4 * min(len(overlap), 3) / 3

        if bonus > 0:
            bonus_count += 1

        adjusted.append(FactorScores(
            symbol=s.symbol,
            momentum_short=s.momentum_short,
            momentum_mid=s.momentum_mid,
            trend=s.trend,
            volume=s.volume,
            volatility=s.volatility,
            relative_strength=s.relative_strength,
            composite=s.composite + bonus,
        ))

    adjusted.sort(key=lambda x: x.composite, reverse=True)
    logger.info("L3 resonance: %d stocks received bonus", bonus_count)
    return adjusted
