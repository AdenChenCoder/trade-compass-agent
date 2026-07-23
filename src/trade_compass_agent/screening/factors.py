"""L2 multi-factor scoring — rank stocks by composite score.

Each stock gets a percentile-based score across multiple dimensions.
No hard thresholds; output is a score 0.0-1.0 per stock.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np
import pandas as pd

from trade_compass_agent.screening.config import ScreeningConfig

logger = logging.getLogger(__name__)


@dataclass
class FactorScores:
    symbol: str
    momentum_short: float  # 5d return percentile
    momentum_mid: float  # 20d return percentile
    trend: float  # MA alignment + slope
    volume: float  # volume ratio percentile
    volatility: float  # ATR compression (inverted)
    relative_strength: float  # RS vs benchmark
    composite: float  # weighted sum


def compute_factor_scores(
    symbols: list[str],
    df_map: dict[str, pd.DataFrame],
    cfg: ScreeningConfig,
    *,
    benchmark_df: pd.DataFrame | None = None,
) -> list[FactorScores]:
    """Compute L2 factor scores for all symbols.

    Each factor is computed as a cross-sectional percentile (0.0-1.0).
    Composite = weighted sum of all factors.
    """
    raw: dict[str, dict[str, float]] = {}

    for symbol in symbols:
        df = df_map.get(symbol)
        if df is None or len(df) < 20:
            continue
        scores = _compute_single(df, benchmark_df)
        if scores:
            raw[symbol] = scores

    if not raw:
        return []

    all_symbols = list(raw.keys())
    factor_names = ["momentum_short", "momentum_mid", "trend", "volume", "volatility", "relative_strength"]

    percentiles: dict[str, dict[str, float]] = {s: {} for s in all_symbols}
    for factor in factor_names:
        values = np.array([raw[s].get(factor, 0.0) for s in all_symbols])
        ranks = _percentile_rank(values)
        for i, s in enumerate(all_symbols):
            percentiles[s][factor] = ranks[i]

    weights = {
        "momentum_short": cfg.w_momentum_short,
        "momentum_mid": cfg.w_momentum_mid,
        "trend": cfg.w_trend,
        "volume": cfg.w_volume,
        "volatility": cfg.w_volatility,
        "relative_strength": cfg.w_relative_strength,
    }

    results: list[FactorScores] = []
    for s in all_symbols:
        p = percentiles[s]
        composite = sum(p.get(f, 0.0) * weights[f] for f in factor_names)
        results.append(FactorScores(
            symbol=s,
            momentum_short=p.get("momentum_short", 0.0),
            momentum_mid=p.get("momentum_mid", 0.0),
            trend=p.get("trend", 0.0),
            volume=p.get("volume", 0.0),
            volatility=p.get("volatility", 0.0),
            relative_strength=p.get("relative_strength", 0.0),
            composite=composite,
        ))

    results.sort(key=lambda x: x.composite, reverse=True)
    logger.info("L2 scoring: %d stocks scored", len(results))
    return results


def _compute_single(
    df: pd.DataFrame,
    benchmark_df: pd.DataFrame | None,
) -> dict[str, float] | None:
    """Compute raw factor values for a single stock."""
    close = df["close"].values
    if len(close) < 20:
        return None

    ret_5d = (close[-1] / close[-5] - 1) if len(close) >= 5 and close[-5] > 0 else 0.0
    ret_20d = (close[-1] / close[-20] - 1) if len(close) >= 20 and close[-20] > 0 else 0.0

    ma5 = float(np.mean(close[-5:])) if len(close) >= 5 else close[-1]
    ma10 = float(np.mean(close[-10:])) if len(close) >= 10 else close[-1]
    ma20 = float(np.mean(close[-20:])) if len(close) >= 20 else close[-1]

    trend_score = 0.0
    if ma5 > ma10:
        trend_score += 0.33
    if ma10 > ma20:
        trend_score += 0.33
    if len(close) >= 25:
        ma20_prev = float(np.mean(close[-25:-5]))
        slope = (ma20 - ma20_prev) / ma20_prev if ma20_prev > 0 else 0
        trend_score += min(max(slope * 10, 0), 0.34)

    volume = df["volume"].values if "volume" in df.columns else np.ones(len(close))
    vol_5d = float(np.mean(volume[-5:])) if len(volume) >= 5 else 1.0
    vol_20d = float(np.mean(volume[-20:])) if len(volume) >= 20 else 1.0
    vol_ratio = vol_5d / vol_20d if vol_20d > 0 else 1.0

    high = df["high"].values if "high" in df.columns else close
    low = df["low"].values if "low" in df.columns else close
    atr_5 = float(np.mean(np.abs(high[-5:] - low[-5:])))
    atr_20 = float(np.mean(np.abs(high[-20:] - low[-20:]))) if len(high) >= 20 else atr_5
    volatility_compression = 1.0 - (atr_5 / atr_20 if atr_20 > 0 else 1.0)
    volatility_compression = max(0.0, min(1.0, volatility_compression))

    rs = 0.0
    if benchmark_df is not None and len(benchmark_df) >= 10:
        bench_close = benchmark_df["close"].values
        stock_ret_10d = (close[-1] / close[-10] - 1) if len(close) >= 10 and close[-10] > 0 else 0
        bench_ret_10d = (bench_close[-1] / bench_close[-10] - 1) if bench_close[-10] > 0 else 0
        rs = stock_ret_10d - bench_ret_10d

    return {
        "momentum_short": ret_5d,
        "momentum_mid": ret_20d,
        "trend": trend_score,
        "volume": vol_ratio,
        "volatility": volatility_compression,
        "relative_strength": rs,
    }


def _percentile_rank(values: np.ndarray) -> np.ndarray:
    """Convert raw values to 0-1 percentile ranks."""
    n = len(values)
    if n == 0:
        return values
    if n == 1:
        return np.array([0.5])
    order = values.argsort().argsort()
    return order / (n - 1)
