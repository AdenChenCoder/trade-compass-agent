"""Kronos K-line foundation model adapter — wraps model loading and inference.

Provides a thin adapter between trade-compass-agent's Bar objects and
Kronos's KronosPredictor API. Uses lazy loading so the heavy torch
dependency is only imported when actually needed.

Install: pip install -e ".[forecast]"
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import pandas as pd

from trade_compass_agent.domain.models import Bar

logger = logging.getLogger(__name__)

# Model → HuggingFace IDs
_MODEL_REGISTRY: dict[str, dict[str, str]] = {
    "mini": {
        "model": "NeoQuasar/Kronos-mini",
        "tokenizer": "NeoQuasar/Kronos-Tokenizer-2k",
        "max_context": 2048,
    },
    "small": {
        "model": "NeoQuasar/Kronos-small",
        "tokenizer": "NeoQuasar/Kronos-Tokenizer-base",
        "max_context": 512,
    },
    "base": {
        "model": "NeoQuasar/Kronos-base",
        "tokenizer": "NeoQuasar/Kronos-Tokenizer-base",
        "max_context": 512,
    },
}


@dataclass
class ForecastBar:
    """A predicted OHLCV bar."""
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    amount: float = 0.0


@dataclass
class ForecastResult:
    """Result of a K-line forecast."""
    symbol: str
    model_id: str
    lookback_used: int
    horizon: int
    mean_bars: list[ForecastBar]
    sample_paths: list[list[ForecastBar]] = field(default_factory=list)
    confidence_upper: list[float] = field(default_factory=list)
    confidence_lower: list[float] = field(default_factory=list)


# Singleton predictor cache
_predictor_cache: dict[str, Any] = {}


def _get_predictor(model_size: str = "small") -> Any:
    """Load and cache a KronosPredictor instance."""
    if model_size in _predictor_cache:
        return _predictor_cache[model_size]

    try:
        import torch  # noqa: F401
        from trade_compass_agent.data.kronos import Kronos, KronosTokenizer, KronosPredictor
    except ImportError as exc:
        raise ImportError(
            f"Kronos forecast requires: pip install -e '.[forecast]'. Missing: {exc}"
        ) from exc

    reg = _MODEL_REGISTRY.get(model_size)
    if not reg:
        raise ValueError(f"Unknown model size: {model_size}. Available: {list(_MODEL_REGISTRY)}")

    logger.info("Loading Kronos model: %s", reg["model"])
    tokenizer = KronosTokenizer.from_pretrained(reg["tokenizer"])
    model = Kronos.from_pretrained(reg["model"])
    predictor = KronosPredictor(model, tokenizer, max_context=int(reg["max_context"]))
    _predictor_cache[model_size] = predictor
    logger.info("Kronos model loaded: %s (device: %s)", reg["model"], predictor.device)
    return predictor


def _bars_to_df(bars: list[Bar]) -> tuple[pd.DataFrame, pd.Series]:
    """Convert Bar objects to the DataFrame + timestamp Series Kronos expects."""
    records = []
    timestamps = []
    for b in bars:
        ohlc_mean = (b.open + b.high + b.low + b.close) / 4
        records.append({
            "open": b.open,
            "high": b.high,
            "low": b.low,
            "close": b.close,
            "volume": b.volume,
            "amount": b.amount if b.amount else b.volume * ohlc_mean,
        })
        timestamps.append(b.timestamp)

    df = pd.DataFrame(records)
    ts = pd.Series(timestamps)
    return df, ts


def _infer_freq(bars: list[Bar]) -> timedelta:
    """Infer bar frequency from the last few bars."""
    if len(bars) < 2:
        return timedelta(days=1)
    deltas = [bars[i].timestamp - bars[i - 1].timestamp for i in range(-1, -min(6, len(bars)), -1)]
    median_delta = sorted(deltas)[len(deltas) // 2]
    if median_delta < timedelta(hours=1):
        return median_delta
    return timedelta(days=1)


def _generate_future_timestamps(last_ts: datetime, freq: timedelta, n: int) -> pd.Series:
    """Generate n future timestamps based on frequency."""
    future = []
    current = last_ts
    for _ in range(n):
        current = current + freq
        # Skip weekends for daily bars
        if freq >= timedelta(days=1):
            while current.weekday() >= 5:
                current += timedelta(days=1)
        future.append(current)
    return pd.Series(future)


def forecast_kline(
    bars: list[Bar],
    symbol: str,
    *,
    horizon: int = 10,
    model_size: str = "small",
    sample_count: int = 5,
    temperature: float = 0.8,
    top_p: float = 0.9,
) -> ForecastResult:
    """Run Kronos K-line forecast on historical bars.

    Args:
        bars: Historical OHLCV bars (at least 30, ideally 60-400).
        symbol: Stock symbol for metadata.
        horizon: Number of future bars to predict.
        model_size: "mini", "small", or "base".
        sample_count: Number of stochastic sample paths.
        temperature: Sampling temperature (lower = more deterministic).
        top_p: Nucleus sampling threshold.

    Returns:
        ForecastResult with mean forecast bars and sample paths.
    """
    if len(bars) < 30:
        raise ValueError(f"Need at least 30 historical bars, got {len(bars)}")

    predictor = _get_predictor(model_size)

    # Truncate to max context
    max_ctx = int(_MODEL_REGISTRY[model_size]["max_context"])
    if len(bars) > max_ctx:
        bars = bars[-max_ctx:]

    x_df, x_timestamp = _bars_to_df(bars)
    freq = _infer_freq(bars)
    y_timestamp = _generate_future_timestamps(bars[-1].timestamp, freq, horizon)

    import numpy as np

    # Run multiple sample paths
    all_paths: list[list[ForecastBar]] = []
    all_close_arrays: list[list[float]] = []

    for _ in range(sample_count):
        pred_df = predictor.predict(
            df=x_df,
            x_timestamp=x_timestamp,
            y_timestamp=y_timestamp,
            pred_len=horizon,
            T=temperature,
            top_p=top_p,
            top_k=0,
            sample_count=1,
            verbose=False,
        )

        path_bars = []
        closes = []
        for i, row in pred_df.iterrows():
            fb = ForecastBar(
                timestamp=y_timestamp.iloc[pred_df.index.get_loc(i)] if isinstance(i, int) else i,
                open=round(float(row["open"]), 3),
                high=round(float(row["high"]), 3),
                low=round(float(row["low"]), 3),
                close=round(float(row["close"]), 3),
                volume=max(0, round(float(row["volume"]), 0)),
                amount=max(0, round(float(row.get("amount", 0)), 0)),
            )
            path_bars.append(fb)
            closes.append(fb.close)
        all_paths.append(path_bars)
        all_close_arrays.append(closes)

    close_matrix = np.array(all_close_arrays)

    # Mean of all OHLCV across paths
    mean_bars = []
    for step in range(horizon):
        opens = [p[step].open for p in all_paths]
        highs = [p[step].high for p in all_paths]
        lows = [p[step].low for p in all_paths]
        closes_step = [p[step].close for p in all_paths]
        volumes = [p[step].volume for p in all_paths]

        mean_bars.append(ForecastBar(
            timestamp=all_paths[0][step].timestamp,
            open=round(float(np.mean(opens)), 3),
            high=round(float(np.mean(highs)), 3),
            low=round(float(np.mean(lows)), 3),
            close=round(float(np.mean(closes_step)), 3),
            volume=round(float(np.mean(volumes)), 0),
        ))

    # Confidence band (close prices)
    upper = [round(float(v), 3) for v in np.max(close_matrix, axis=0)]
    lower = [round(float(v), 3) for v in np.min(close_matrix, axis=0)]

    return ForecastResult(
        symbol=symbol,
        model_id=_MODEL_REGISTRY[model_size]["model"],
        lookback_used=len(bars),
        horizon=horizon,
        mean_bars=mean_bars,
        sample_paths=all_paths,
        confidence_upper=upper,
        confidence_lower=lower,
    )


def is_kronos_available() -> bool:
    """Check if Kronos dependencies (PyTorch + vendored model code) are available."""
    try:
        import torch  # noqa: F401
        from trade_compass_agent.data.kronos import Kronos  # noqa: F401
        return True
    except ImportError:
        return False
