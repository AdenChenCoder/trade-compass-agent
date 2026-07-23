"""Screening engine — orchestrates L1-L4 and produces final ranked candidates."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

import pandas as pd

from trade_compass_agent.screening.config import ScreeningConfig
from trade_compass_agent.screening.factors import FactorScores, compute_factor_scores
from trade_compass_agent.screening.filters import layer1_filter
from trade_compass_agent.screening.resonance import apply_resonance_bonus
from trade_compass_agent.screening.triggers import apply_trigger_bonus
from trade_compass_agent.screening.universe import StockInfo, filter_st, resolve_universe

logger = logging.getLogger(__name__)


@dataclass
class ScreeningResult:
    timestamp: str
    universe_size: int
    l1_passed: int
    l1_rejected: int
    scored_count: int
    candidates: list[FactorScores]
    top_n: list[FactorScores]
    cfg: ScreeningConfig = field(repr=False)


def run_screening(
    df_map: dict[str, pd.DataFrame],
    cfg: ScreeningConfig | None = None,
    *,
    stocks: list[StockInfo] | None = None,
    market_cap_map: dict[str, float] | None = None,
    benchmark_df: pd.DataFrame | None = None,
    hot_industries: list[str] | None = None,
    hot_concepts: list[str] | None = None,
    industry_map: dict[str, str] | None = None,
    concept_map: dict[str, list[str]] | None = None,
) -> ScreeningResult:
    """Run the full screening pipeline: L1 → L2 → L3 → L4 → top-N.

    Args:
        df_map: Pre-fetched OHLCV data {symbol: DataFrame}
        cfg: Screening configuration
        stocks: Universe (auto-resolved if None)
        market_cap_map: Optional market cap data for L1
        benchmark_df: Benchmark index DataFrame for relative strength
        hot_industries: Current hot industry names for L3
        hot_concepts: Current hot concept names for L3
        industry_map: {symbol: industry_name}
        concept_map: {symbol: [concept_names]}

    Returns:
        ScreeningResult with ranked candidates
    """
    if cfg is None:
        cfg = ScreeningConfig.from_env()

    if stocks is None:
        stocks = resolve_universe(cfg.boards)
        if cfg.exclude_st:
            stocks = filter_st(stocks)

    logger.info("Screening started: universe=%d stocks, data=%d symbols", len(stocks), len(df_map))

    # L1: Hard filter
    l1 = layer1_filter(stocks, df_map, cfg, market_cap_map=market_cap_map)

    # L2: Multi-factor scoring
    scores = compute_factor_scores(l1.passed, df_map, cfg, benchmark_df=benchmark_df)

    # L3: Sector resonance bonus
    scores = apply_resonance_bonus(
        scores,
        hot_industries=hot_industries,
        hot_concepts=hot_concepts,
        industry_map=industry_map,
        concept_map=concept_map,
        cfg=cfg,
    )

    # L4: Technical trigger bonus
    scores = apply_trigger_bonus(scores, df_map, cfg)

    # Top-N selection
    top_n = scores[: cfg.top_n]

    result = ScreeningResult(
        timestamp=datetime.now().isoformat(timespec="seconds"),
        universe_size=len(stocks),
        l1_passed=len(l1.passed),
        l1_rejected=len(l1.rejected),
        scored_count=len(scores),
        candidates=scores,
        top_n=top_n,
        cfg=cfg,
    )

    logger.info(
        "Screening complete: universe=%d → L1=%d → scored=%d → top_%d=%d",
        result.universe_size,
        result.l1_passed,
        result.scored_count,
        cfg.top_n,
        len(top_n),
    )
    return result
