"""Agent-callable tool for K-line prediction using Kronos foundation model.

Fetches historical bars, runs Kronos inference, and returns a structured
forecast report with predicted OHLCV bars and confidence bands.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from trade_compass_agent.data.kronos_adapter import forecast_install_command
from trade_compass_agent.runtime.market_stack import MarketStack

logger = logging.getLogger(__name__)


def tool_kline_forecast(stack: MarketStack, **kwargs: Any) -> str:
    """Predict future K-line bars using Kronos model.

    Returns JSON with predicted bars, confidence band, and analysis context.
    """
    symbol = str(kwargs.get("symbol") or "")
    horizon = int(kwargs.get("horizon") or 10)
    model_size = str(kwargs.get("model_size") or "small")
    sample_count = int(kwargs.get("sample_count") or 5)
    temperature = float(kwargs.get("temperature") or 0.8)

    if not symbol:
        return json.dumps({"error": "symbol is required"}, ensure_ascii=False)

    # Check availability
    try:
        from trade_compass_agent.data.kronos_adapter import is_kronos_available
        if not is_kronos_available():
            return json.dumps({
                "error": "K线预测功能不可用 — 预测引擎尚未安装。",
                "hint": forecast_install_command(),
            }, ensure_ascii=False)
    except ImportError:
        return json.dumps({"error": "kronos_adapter not found"}, ensure_ascii=False)

    # Fetch historical bars
    provider = stack.provider
    lookback = min(400, int(kwargs.get("lookback") or 120))
    try:
        raw_bars = provider.get_bars(symbol, timeframe="1d", limit=lookback)
    except Exception as exc:
        return json.dumps({"error": f"数据获取失败: {exc}"}, ensure_ascii=False)

    if not raw_bars or len(raw_bars) < 30:
        return json.dumps({
            "error": f"数据不足: {symbol} 仅有 {len(raw_bars) if raw_bars else 0} 条K线，需要至少30条",
        }, ensure_ascii=False)

    # Run forecast
    try:
        from trade_compass_agent.data.kronos_adapter import forecast_kline
        result = forecast_kline(
            bars=raw_bars,
            symbol=symbol,
            horizon=horizon,
            model_size=model_size,
            sample_count=sample_count,
            temperature=temperature,
        )
    except ImportError as exc:
        return json.dumps({
            "error": f"Kronos 依赖未安装: {exc}",
            "hint": forecast_install_command(),
        }, ensure_ascii=False)
    except Exception as exc:
        logger.exception("Kronos forecast failed for %s", symbol)
        return json.dumps({"error": f"预测失败: {exc}"}, ensure_ascii=False)

    # Format response
    last_close = raw_bars[-1].close
    pred_last_close = result.mean_bars[-1].close
    change_pct = round((pred_last_close - last_close) / last_close * 100, 2)

    response = {
        "symbol": symbol,
        "model": result.model_id,
        "lookback_bars": result.lookback_used,
        "horizon_bars": result.horizon,
        "current_close": last_close,
        "forecast_summary": {
            "predicted_close_final": pred_last_close,
            "change_pct": change_pct,
            "direction": "up" if change_pct > 0.5 else ("down" if change_pct < -0.5 else "sideways"),
            "confidence_upper_final": result.confidence_upper[-1] if result.confidence_upper else None,
            "confidence_lower_final": result.confidence_lower[-1] if result.confidence_lower else None,
        },
        "forecast_bars": [
            {
                "timestamp": b.timestamp.isoformat(),
                "open": b.open,
                "high": b.high,
                "low": b.low,
                "close": b.close,
                "volume": b.volume,
            }
            for b in result.mean_bars
        ],
        "confidence_band": {
            "upper": result.confidence_upper,
            "lower": result.confidence_lower,
        },
        "quality_status": "experimental",
        "parameters": {
            "horizon": horizon,
            "model_size": model_size,
            "sample_count": sample_count,
            "lookback": lookback,
        },
        "disclaimer": "⚠️ 模型预测仅供参考，不构成投资建议。实际走势受多重因素影响。",
    }

    return json.dumps(response, ensure_ascii=False, default=str)
