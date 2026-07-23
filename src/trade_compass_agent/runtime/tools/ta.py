from __future__ import annotations

import json

import numpy as np
import pandas as pd

from trade_compass_agent.runtime.market_stack import MarketStack


def _bars_to_df(stack: MarketStack, symbol: str, timeframe: str, limit: int) -> pd.DataFrame:
    bars = stack.provider.get_bars(symbol.strip(), timeframe=timeframe, limit=limit)
    if not bars:
        raise ValueError(f"no bars for {symbol} ({timeframe})")
    df = pd.DataFrame([
        {"ts": b.timestamp, "open": b.open, "high": b.high, "low": b.low,
         "close": b.close, "volume": b.volume}
        for b in bars
    ])
    df.set_index("ts", inplace=True)
    return df


def tool_compute_rsi(
    stack: MarketStack,
    *,
    symbol: str,
    timeframe: str = "1d",
    period: int = 14,
    limit: int = 80,
) -> str:
    df = _bars_to_df(stack, symbol, timeframe, limit)
    delta = df["close"].diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    recent = rsi.iloc[-5:].dropna()
    return json.dumps({
        "symbol": symbol,
        "timeframe": timeframe,
        "period": period,
        "current_rsi": round(float(rsi.iloc[-1]), 2) if not rsi.empty else None,
        "recent_5": [round(float(v), 2) for v in recent],
        "interpretation": _rsi_interpretation(float(rsi.iloc[-1])) if not rsi.empty else "no data",
    }, ensure_ascii=False)


def _rsi_interpretation(value: float) -> str:
    if value >= 80:
        return "extremely_overbought"
    if value >= 70:
        return "overbought"
    if value <= 20:
        return "extremely_oversold"
    if value <= 30:
        return "oversold"
    return "neutral"


def tool_compute_macd(
    stack: MarketStack,
    *,
    symbol: str,
    timeframe: str = "1d",
    fast: int = 12,
    slow: int = 26,
    signal_period: int = 9,
    limit: int = 80,
) -> str:
    df = _bars_to_df(stack, symbol, timeframe, limit)
    ema_fast = df["close"].ewm(span=fast, adjust=False).mean()
    ema_slow = df["close"].ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal_period, adjust=False).mean()
    histogram = macd_line - signal_line
    current = {
        "macd": round(float(macd_line.iloc[-1]), 4),
        "signal": round(float(signal_line.iloc[-1]), 4),
        "histogram": round(float(histogram.iloc[-1]), 4),
    }
    prev_hist = float(histogram.iloc[-2]) if len(histogram) >= 2 else 0.0
    cross = "none"
    if current["histogram"] > 0 and prev_hist <= 0:
        cross = "golden_cross"
    elif current["histogram"] < 0 and prev_hist >= 0:
        cross = "death_cross"
    return json.dumps({
        "symbol": symbol,
        "timeframe": timeframe,
        "params": {"fast": fast, "slow": slow, "signal": signal_period},
        "current": current,
        "cross": cross,
        "trend": "bullish" if current["macd"] > 0 else "bearish",
    }, ensure_ascii=False)


def tool_compute_bollinger(
    stack: MarketStack,
    *,
    symbol: str,
    timeframe: str = "1d",
    period: int = 20,
    std_dev: float = 2.0,
    limit: int = 60,
) -> str:
    df = _bars_to_df(stack, symbol, timeframe, limit)
    middle = df["close"].rolling(period).mean()
    rolling_std = df["close"].rolling(period).std()
    upper = middle + std_dev * rolling_std
    lower = middle - std_dev * rolling_std
    last_close = float(df["close"].iloc[-1])
    last_upper = float(upper.iloc[-1]) if not upper.empty else 0
    last_lower = float(lower.iloc[-1]) if not lower.empty else 0
    last_middle = float(middle.iloc[-1]) if not middle.empty else 0
    bandwidth = (last_upper - last_lower) / last_middle if last_middle else 0
    pct_b = (last_close - last_lower) / (last_upper - last_lower) if (last_upper - last_lower) else 0.5
    return json.dumps({
        "symbol": symbol,
        "timeframe": timeframe,
        "period": period,
        "current": {
            "upper": round(last_upper, 3),
            "middle": round(last_middle, 3),
            "lower": round(last_lower, 3),
            "close": round(last_close, 3),
        },
        "bandwidth": round(bandwidth, 4),
        "pct_b": round(pct_b, 4),
        "position": _bollinger_position(pct_b),
    }, ensure_ascii=False)


def _bollinger_position(pct_b: float) -> str:
    if pct_b >= 1.0:
        return "above_upper"
    if pct_b >= 0.8:
        return "near_upper"
    if pct_b <= 0.0:
        return "below_lower"
    if pct_b <= 0.2:
        return "near_lower"
    return "middle"


def tool_compute_volume_ratio(
    stack: MarketStack,
    *,
    symbol: str,
    timeframe: str = "1d",
    period: int = 5,
    limit: int = 30,
) -> str:
    df = _bars_to_df(stack, symbol, timeframe, limit)
    vol_ma = df["volume"].rolling(period).mean()
    current_vol = float(df["volume"].iloc[-1])
    avg_vol = float(vol_ma.iloc[-1]) if not vol_ma.empty else current_vol
    ratio = current_vol / avg_vol if avg_vol > 0 else 1.0
    return json.dumps({
        "symbol": symbol,
        "timeframe": timeframe,
        "period": period,
        "current_volume": int(current_vol),
        "avg_volume": int(avg_vol),
        "volume_ratio": round(ratio, 3),
        "interpretation": _volume_interpretation(ratio),
    }, ensure_ascii=False)


def _volume_interpretation(ratio: float) -> str:
    if ratio >= 3.0:
        return "extreme_expansion"
    if ratio >= 2.0:
        return "strong_expansion"
    if ratio >= 1.5:
        return "moderate_expansion"
    if ratio <= 0.5:
        return "extreme_shrinkage"
    if ratio <= 0.7:
        return "shrinkage"
    return "normal"


def tool_compute_ma(
    stack: MarketStack,
    *,
    symbol: str,
    timeframe: str = "1d",
    periods: str = "5,10,20,60",
    limit: int = 120,
) -> str:
    """Deterministic moving average computation. periods: comma-separated integers."""
    period_list = [int(p.strip()) for p in periods.split(",") if p.strip().isdigit()]
    if not period_list:
        return json.dumps({"error": "invalid periods parameter"}, ensure_ascii=False)
    df = _bars_to_df(stack, symbol, timeframe, limit)
    closes = df["close"]
    last_close = round(float(closes.iloc[-1]), 3)
    ma_values = {}
    for period in period_list:
        if len(closes) >= period:
            ma_val = float(closes.iloc[-period:].mean())
            ma_values[f"MA{period}"] = round(ma_val, 3)
    positions = {}
    for key, val in ma_values.items():
        if last_close > val:
            positions[key] = "above"
        elif last_close < val:
            positions[key] = "below"
        else:
            positions[key] = "at"
    return json.dumps({
        "symbol": symbol,
        "timeframe": timeframe,
        "last_close": last_close,
        "ma": ma_values,
        "position_vs_ma": positions,
        "trend": _ma_trend_summary(ma_values),
    }, ensure_ascii=False)


def _ma_trend_summary(ma_values: dict[str, float]) -> str:
    sorted_mas = sorted(ma_values.items(), key=lambda kv: int(kv[0][2:]))
    if len(sorted_mas) < 2:
        return "insufficient_data"
    values = [v for _, v in sorted_mas]
    if all(values[i] >= values[i + 1] for i in range(len(values) - 1)):
        return "bullish_alignment"
    if all(values[i] <= values[i + 1] for i in range(len(values) - 1)):
        return "bearish_alignment"
    return "mixed"
