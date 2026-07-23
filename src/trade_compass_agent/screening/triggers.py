"""L4 technical trigger signals — bonus scoring for actionable setups."""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from trade_compass_agent.screening.config import ScreeningConfig
from trade_compass_agent.screening.factors import FactorScores

logger = logging.getLogger(__name__)


def apply_trigger_bonus(
    scores: list[FactorScores],
    df_map: dict[str, pd.DataFrame],
    cfg: ScreeningConfig,
) -> list[FactorScores]:
    """Add bonus for stocks showing actionable technical triggers.

    Triggers checked:
    - MACD golden cross (last 3 days)
    - RSI pullback to 40-55 zone from above
    - Bollinger band lower touch + bounce
    - Volume breakout (today vol > 2x 20d avg)
    """
    adjusted: list[FactorScores] = []
    trigger_count = 0

    for s in scores:
        df = df_map.get(s.symbol)
        if df is None or len(df) < 26:
            adjusted.append(s)
            continue

        bonus = 0.0
        close = df["close"].values
        volume = df["volume"].values if "volume" in df.columns else None

        if _check_macd_cross(close):
            bonus += cfg.trigger_bonus_macd

        if _check_rsi_pullback(close):
            bonus += cfg.trigger_bonus_rsi

        if _check_bollinger_bounce(close):
            bonus += cfg.trigger_bonus_bollinger

        if volume is not None and _check_volume_breakout(volume):
            bonus += cfg.trigger_bonus_volume_breakout

        if bonus > 0:
            trigger_count += 1

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
    logger.info("L4 triggers: %d stocks received bonus", trigger_count)
    return adjusted


def _check_macd_cross(close: np.ndarray) -> bool:
    """Check if MACD crossed above signal in last 3 bars."""
    if len(close) < 26:
        return False
    ema12 = _ema(close, 12)
    ema26 = _ema(close, 26)
    macd_line = ema12 - ema26
    signal_line = _ema(macd_line[-9:], 9) if len(macd_line) >= 9 else macd_line[-1:]

    if len(signal_line) < 2:
        return False
    macd_recent = macd_line[-3:]
    sig_val = signal_line[-1]
    return bool(macd_recent[-1] > sig_val and macd_recent[0] <= sig_val)


def _check_rsi_pullback(close: np.ndarray, period: int = 14) -> bool:
    """Check if RSI pulled back to 40-55 zone (healthy consolidation)."""
    if len(close) < period + 5:
        return False
    rsi = _compute_rsi(close, period)
    if len(rsi) < 5:
        return False
    current_rsi = rsi[-1]
    recent_high = max(rsi[-5:])
    return bool(40 <= current_rsi <= 55 and recent_high > 60)


def _check_bollinger_bounce(close: np.ndarray, period: int = 20) -> bool:
    """Check if price touched lower band and bounced in last 3 days."""
    if len(close) < period + 3:
        return False
    ma = np.mean(close[-period:])
    std = np.std(close[-period:])
    lower = ma - 2 * std

    recent = close[-3:]
    return bool(min(recent) <= lower * 1.01 and recent[-1] > lower)


def _check_volume_breakout(volume: np.ndarray) -> bool:
    """Check if latest volume > 2x 20-day average."""
    if len(volume) < 21:
        return False
    avg_20 = np.mean(volume[-21:-1])
    return bool(avg_20 > 0 and volume[-1] > 2.0 * avg_20)


def _ema(data: np.ndarray, period: int) -> np.ndarray:
    """Simple EMA calculation."""
    if len(data) < period:
        return data
    alpha = 2.0 / (period + 1)
    result = np.empty_like(data, dtype=float)
    result[0] = data[0]
    for i in range(1, len(data)):
        result[i] = alpha * data[i] + (1 - alpha) * result[i - 1]
    return result


def _compute_rsi(close: np.ndarray, period: int = 14) -> np.ndarray:
    """Compute RSI series."""
    deltas = np.diff(close)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)

    avg_gain = np.mean(gains[:period])
    avg_loss = np.mean(losses[:period])

    rsi_values = []
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        rs = avg_gain / avg_loss if avg_loss > 0 else 100.0
        rsi_values.append(100.0 - 100.0 / (1.0 + rs))

    return np.array(rsi_values)
