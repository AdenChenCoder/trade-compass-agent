"""Visual pattern recognition specialist — K-line chart → Vision LLM → pattern report.

Pipeline:
1. Compute quantitative TA indicators (MA, MACD, RSI, Bollinger)
2. Render enriched candlestick chart as PNG (mplfinance + Bollinger bands)
3. Encode to base64
4. Send to vision-capable LLM with indicator context + structured prompt
5. Return structured pattern analysis

Transforms a chart into a multimodal message and a structured pattern report.
"""

from __future__ import annotations

import base64
import io
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from trade_compass_agent.config import AppConfig
from trade_compass_agent.llm.providers import ChatClient, ChatMessage, create_chat_client, create_vision_client
from trade_compass_agent.runtime.exceptions import AgentUnavailableError
from trade_compass_agent.runtime.market_stack import MarketStack

logger = logging.getLogger(__name__)

_HEADLESS_MATPLOTLIB_BACKEND = "Agg"
_MPLCONFIGDIR = Path(tempfile.gettempdir()) / "trade-compass-matplotlib"

# ---------------------------------------------------------------------------
# Quantitative indicator helpers (inline to avoid circular imports)
# ---------------------------------------------------------------------------

def _compute_indicators(df: pd.DataFrame) -> dict[str, Any]:
    """Compute TA indicators from OHLCV DataFrame. Returns dict summary."""
    closes = df["close"]
    n = len(closes)
    result: dict[str, Any] = {}

    # Moving averages
    ma_periods = [5, 10, 20, 60]
    ma_vals: dict[str, float] = {}
    for p in ma_periods:
        if n >= p:
            ma_vals[f"MA{p}"] = round(float(closes.iloc[-p:].mean()), 3)
    result["ma"] = ma_vals

    last_close = float(closes.iloc[-1])
    result["last_close"] = round(last_close, 3)

    ma_positions = {}
    for k, v in ma_vals.items():
        ma_positions[k] = "above" if last_close > v else ("below" if last_close < v else "at")
    result["ma_position"] = ma_positions

    # RSI-14
    if n >= 20:
        delta = closes.diff()
        gain = delta.clip(lower=0)
        loss = (-delta).clip(lower=0)
        avg_gain = gain.ewm(alpha=1 / 14, min_periods=14).mean()
        avg_loss = loss.ewm(alpha=1 / 14, min_periods=14).mean()
        last_gain = float(avg_gain.iloc[-1])
        last_loss = float(avg_loss.iloc[-1])
        if last_loss == 0:
            rsi_val = 100.0 if last_gain > 0 else 50.0
        else:
            rs = last_gain / last_loss
            rsi_val = 100 - (100 / (1 + rs))
        if not np.isnan(rsi_val):
            result["rsi_14"] = round(rsi_val, 2)
            if rsi_val >= 70:
                result["rsi_zone"] = "overbought"
            elif rsi_val <= 30:
                result["rsi_zone"] = "oversold"
            else:
                result["rsi_zone"] = "neutral"

    # MACD (12, 26, 9)
    if n >= 35:
        ema12 = closes.ewm(span=12, adjust=False).mean()
        ema26 = closes.ewm(span=26, adjust=False).mean()
        macd_line = ema12 - ema26
        signal_line = macd_line.ewm(span=9, adjust=False).mean()
        histogram = macd_line - signal_line
        result["macd"] = {
            "macd": round(float(macd_line.iloc[-1]), 4),
            "signal": round(float(signal_line.iloc[-1]), 4),
            "histogram": round(float(histogram.iloc[-1]), 4),
        }
        prev_hist = float(histogram.iloc[-2]) if len(histogram) >= 2 else 0
        cur_hist = float(histogram.iloc[-1])
        if cur_hist > 0 and prev_hist <= 0:
            result["macd_cross"] = "golden_cross"
        elif cur_hist < 0 and prev_hist >= 0:
            result["macd_cross"] = "death_cross"
        else:
            result["macd_cross"] = "none"

    # Bollinger Bands (20, 2)
    if n >= 20:
        mid = closes.rolling(20).mean()
        std = closes.rolling(20).std()
        upper = mid + 2 * std
        lower = mid - 2 * std
        last_upper = float(upper.iloc[-1])
        last_lower = float(lower.iloc[-1])
        last_mid = float(mid.iloc[-1])
        pct_b = (last_close - last_lower) / (last_upper - last_lower) if (last_upper - last_lower) > 0 else 0.5
        bw = (last_upper - last_lower) / last_mid if last_mid else 0
        result["bollinger"] = {
            "upper": round(last_upper, 3),
            "middle": round(last_mid, 3),
            "lower": round(last_lower, 3),
            "pct_b": round(pct_b, 4),
            "bandwidth": round(bw, 4),
        }

    # KDJ (9, 3, 3)
    if n >= 12 and "high" in df.columns and "low" in df.columns:
        highs = df["high"]
        lows = df["low"]
        rsv_period = 9
        low_n = lows.rolling(rsv_period).min()
        high_n = highs.rolling(rsv_period).max()
        denom = high_n - low_n
        rsv = ((closes - low_n) / denom.replace(0, np.nan)) * 100
        rsv = rsv.fillna(50)
        k_vals = rsv.ewm(com=2, adjust=False).mean()
        d_vals = k_vals.ewm(com=2, adjust=False).mean()
        j_vals = 3 * k_vals - 2 * d_vals
        k_val = float(k_vals.iloc[-1])
        d_val = float(d_vals.iloc[-1])
        j_val = float(j_vals.iloc[-1])
        if not any(np.isnan(v) for v in (k_val, d_val, j_val)):
            result["kdj"] = {
                "K": round(k_val, 2),
                "D": round(d_val, 2),
                "J": round(j_val, 2),
            }
            # Cross detection
            prev_k = float(k_vals.iloc[-2]) if len(k_vals) >= 2 else k_val
            prev_d = float(d_vals.iloc[-2]) if len(d_vals) >= 2 else d_val
            if k_val > d_val and prev_k <= prev_d:
                result["kdj_cross"] = "golden_cross"
            elif k_val < d_val and prev_k >= prev_d:
                result["kdj_cross"] = "death_cross"
            else:
                result["kdj_cross"] = "none"
            # Zone
            if j_val >= 100:
                result["kdj_zone"] = "overbought"
            elif j_val <= 0:
                result["kdj_zone"] = "oversold"
            else:
                result["kdj_zone"] = "neutral"

    # VWAP (typical price * volume / cumulative volume)
    if n >= 5 and "volume" in df.columns and "high" in df.columns and "low" in df.columns:
        typical = (df["high"] + df["low"] + df["close"]) / 3
        cum_tp_vol = (typical * df["volume"]).cumsum()
        cum_vol = df["volume"].cumsum()
        vwap_series = cum_tp_vol / cum_vol.replace(0, np.nan)
        vwap_val = float(vwap_series.iloc[-1])
        if not np.isnan(vwap_val):
            result["vwap"] = round(vwap_val, 3)
            result["vwap_deviation"] = round((last_close - vwap_val) / vwap_val * 100, 2)

    # Volume ratio (5-day)
    if n >= 6 and "volume" in df.columns:
        vol = df["volume"]
        avg_vol = float(vol.iloc[-6:-1].mean())
        cur_vol = float(vol.iloc[-1])
        result["volume_ratio"] = round(cur_vol / avg_vol, 3) if avg_vol > 0 else 1.0

    # Trendline detection (swing high/low points)
    if n >= 15 and "high" in df.columns and "low" in df.columns:
        result["trendlines"] = _detect_trendlines(df)

    return result


def _ensure_headless_matplotlib_backend() -> None:
    """Force a non-GUI matplotlib backend before mplfinance imports pyplot."""
    os.environ.setdefault("MPLBACKEND", _HEADLESS_MATPLOTLIB_BACKEND)
    _MPLCONFIGDIR.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("MPLCONFIGDIR", str(_MPLCONFIGDIR))
    try:
        import matplotlib

        backend = str(matplotlib.get_backend()).lower()
        if backend != _HEADLESS_MATPLOTLIB_BACKEND.lower():
            matplotlib.use(_HEADLESS_MATPLOTLIB_BACKEND, force=True)
    except Exception as exc:
        logger.debug("Unable to enforce matplotlib backend: %s", exc)


def _detect_trendlines(df: pd.DataFrame, lookback: int = 5) -> dict[str, Any]:
    """Detect support/resistance trendlines from swing points.

    Uses simple local extrema detection over `lookback` window.
    """
    highs = df["high"].values
    lows = df["low"].values
    n = len(highs)

    swing_highs: list[tuple[int, float]] = []
    swing_lows: list[tuple[int, float]] = []

    for i in range(lookback, n - lookback):
        if highs[i] == max(highs[i - lookback:i + lookback + 1]):
            swing_highs.append((i, float(highs[i])))
        if lows[i] == min(lows[i - lookback:i + lookback + 1]):
            swing_lows.append((i, float(lows[i])))

    result: dict[str, Any] = {
        "swing_high_count": len(swing_highs),
        "swing_low_count": len(swing_lows),
    }

    # Resistance trendline: last 2 swing highs
    if len(swing_highs) >= 2:
        h1, h2 = swing_highs[-2], swing_highs[-1]
        slope = (h2[1] - h1[1]) / (h2[0] - h1[0]) if h2[0] != h1[0] else 0
        result["resistance_trend"] = "descending" if slope < -0.01 else ("ascending" if slope > 0.01 else "flat")
        result["resistance_points"] = [round(h1[1], 3), round(h2[1], 3)]

    # Support trendline: last 2 swing lows
    if len(swing_lows) >= 2:
        l1, l2 = swing_lows[-2], swing_lows[-1]
        slope = (l2[1] - l1[1]) / (l2[0] - l1[0]) if l2[0] != l1[0] else 0
        result["support_trend"] = "descending" if slope < -0.01 else ("ascending" if slope > 0.01 else "flat")
        result["support_points"] = [round(l1[1], 3), round(l2[1], 3)]

    # Channel detection
    if "resistance_trend" in result and "support_trend" in result:
        r_trend = result["resistance_trend"]
        s_trend = result["support_trend"]
        if r_trend == s_trend == "descending":
            result["channel"] = "下降通道"
        elif r_trend == s_trend == "ascending":
            result["channel"] = "上升通道"
        elif r_trend == s_trend == "flat":
            result["channel"] = "横盘通道"
        elif r_trend == "flat" and s_trend == "ascending":
            result["channel"] = "上升三角形"
        elif r_trend == "descending" and s_trend == "flat":
            result["channel"] = "下降三角形"
        else:
            result["channel"] = "收敛/楔形"

    return result


def _format_indicator_context(indicators: dict[str, Any], symbol: str) -> str:
    """Format indicators into a concise text block for the LLM prompt."""
    lines = [f"## {symbol} 量化指标摘要"]
    lines.append(f"- 最新收盘价: {indicators['last_close']}")

    if indicators.get("ma"):
        ma_parts = [f"{k}={v}" for k, v in indicators["ma"].items()]
        lines.append(f"- 均线: {', '.join(ma_parts)}")
        pos_parts = [f"{k}: {v}" for k, v in indicators.get("ma_position", {}).items()]
        lines.append(f"- 价格相对均线: {', '.join(pos_parts)}")

    if "rsi_14" in indicators:
        lines.append(f"- RSI(14): {indicators['rsi_14']} ({indicators['rsi_zone']})")

    if "macd" in indicators:
        m = indicators["macd"]
        lines.append(f"- MACD: DIF={m['macd']}, DEA={m['signal']}, 柱={m['histogram']}")
        if indicators.get("macd_cross") != "none":
            lines.append(f"- MACD信号: {indicators['macd_cross']}")

    if "kdj" in indicators:
        k = indicators["kdj"]
        lines.append(f"- KDJ: K={k['K']}, D={k['D']}, J={k['J']} ({indicators.get('kdj_zone', 'neutral')})")
        if indicators.get("kdj_cross") != "none":
            lines.append(f"- KDJ信号: {indicators['kdj_cross']}")

    if "bollinger" in indicators:
        b = indicators["bollinger"]
        bw_str = f", 带宽={b['bandwidth']}" if "bandwidth" in b else ""
        lines.append(f"- 布林带: 上轨={b['upper']}, 中轨={b['middle']}, 下轨={b['lower']}, %B={b['pct_b']}{bw_str}")

    if "vwap" in indicators:
        dev = indicators.get("vwap_deviation", 0)
        dev_label = f"+{dev}%" if dev >= 0 else f"{dev}%"
        lines.append(f"- VWAP: {indicators['vwap']} (偏离: {dev_label})")

    if "volume_ratio" in indicators:
        lines.append(f"- 量比(5日): {indicators['volume_ratio']}")

    if "trendlines" in indicators:
        t = indicators["trendlines"]
        parts = []
        if "channel" in t:
            parts.append(f"通道形态: {t['channel']}")
        if "resistance_trend" in t:
            pts = t.get("resistance_points", [])
            parts.append(f"阻力趋势线: {t['resistance_trend']} {pts}")
        if "support_trend" in t:
            pts = t.get("support_points", [])
            parts.append(f"支撑趋势线: {t['support_trend']} {pts}")
        if parts:
            lines.append(f"- 趋势线: {'; '.join(parts)}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Chart rendering
# ---------------------------------------------------------------------------

_PATTERN_PROMPT = """\
This is a {timeframe} candlestick chart for stock {symbol} (last {bars} trading bars).
The chart shows: candlesticks, MA5/MA10/MA20 lines, Bollinger Bands (gray shading), and volume bars \
(green = up day, red = down day).

{indicator_context}

Analyze the chart with the quantitative context above. Follow this structure exactly:

## 1. 形态识别
List each detected classical pattern with:
- Pattern name (Chinese + English)
- Completeness: forming / confirmed / failed
- Confidence: high / medium / low

Check for: 头肩顶/底(H&S), 双顶/双底, W/M底, V形反转, 旗形/楔形, 三角形, 通道, 杯柄, \
吞没, 锤子/上吊, 启明/黄昏星, 十字星

## 2. 关键价位
- 支撑位 (S1, S2): price levels with reasoning
- 阻力位 (R1, R2): price levels with reasoning
- 布林带位置: price vs bands
- VWAP参考: price vs VWAP (if available)
- 趋势线分析: support/resistance trendline implications (if available)

## 3. 趋势判断
- Direction: 上升趋势 / 下降趋势 / 横盘震荡
- Strength: strong / moderate / weak
- MA alignment: 多头排列 / 空头排列 / 交叉缠绕

## 4. 量价关系 & 动量
- Volume trend vs price trend (量价配合 / 量价背离)
- Recent volume characteristics
- KDJ状态: overbought/oversold zone, golden/death cross (if available)
- RSI与KDJ共振: do they agree on direction?

## 5. 综合研判
- Direction: up / down / sideways
- Timeframe: short-term (1-5天) / medium-term (1-4周)
- Confidence: 0.0-1.0
- Key trigger: what event or level break would confirm the move
- Risk factor: primary risk to the thesis

Be specific about price levels. Cross-validate visual patterns against ALL quantitative indicators \
(MA, RSI, MACD, KDJ, Bollinger, VWAP, trendlines).
"""


def render_kline_chart(
    df: pd.DataFrame,
    symbol: str = "",
    bars: int = 40,
) -> str | None:
    """Render enriched candlestick chart as base64 PNG.

    Includes: candlesticks, MA5/10/20, Bollinger Bands (shaded), colored volume bars,
    and a KDJ subplot (panel 2) when sufficient data is available.
    """
    _ensure_headless_matplotlib_backend()
    try:
        import mplfinance as mpf
    except ImportError:
        logger.warning("mplfinance not installed — visual pattern analysis unavailable")
        return None

    chart_df = df.tail(bars).copy()
    if len(chart_df) < 10:
        return None

    chart_df.columns = [c.capitalize() if c != "volume" else "Volume" for c in chart_df.columns]
    if "Open" not in chart_df.columns:
        return None

    chart_df.index = pd.DatetimeIndex(pd.date_range(end="2026-06-16", periods=len(chart_df), freq="B"))
    chart_df.index.name = "Date"

    addplots = []
    has_kdj_panel = False

    try:
        closes = chart_df["Close"]
        n = len(closes)

        # Bollinger Bands — rendered as shaded fill between upper/lower
        if n >= 20:
            bb_mid = closes.rolling(20).mean()
            bb_std = closes.rolling(20).std()
            bb_upper = bb_mid + 2 * bb_std
            bb_lower = bb_mid - 2 * bb_std
            addplots.append(mpf.make_addplot(bb_upper, color="gray", linestyle="--", width=0.6))
            addplots.append(mpf.make_addplot(bb_lower, color="gray", linestyle="--", width=0.6))
            addplots.append(mpf.make_addplot(
                bb_upper, fill_between={"y1": bb_upper.values, "y2": bb_lower.values, "alpha": 0.08, "color": "gray"},
                color="none", width=0,
            ))

        # KDJ subplot (panel 2, below volume)
        if n >= 12 and "High" in chart_df.columns and "Low" in chart_df.columns:
            highs = chart_df["High"]
            lows = chart_df["Low"]
            rsv_period = 9
            low_n = lows.rolling(rsv_period).min()
            high_n = highs.rolling(rsv_period).max()
            denom = high_n - low_n
            rsv = ((closes - low_n) / denom.replace(0, np.nan)) * 100
            rsv = rsv.fillna(50)
            k_line = rsv.ewm(com=2, adjust=False).mean()
            d_line = k_line.ewm(com=2, adjust=False).mean()
            j_line = 3 * k_line - 2 * d_line

            addplots.append(mpf.make_addplot(k_line, panel=2, color="#f59e0b", width=0.8, ylabel="KDJ"))
            addplots.append(mpf.make_addplot(d_line, panel=2, color="#3b82f6", width=0.8))
            addplots.append(mpf.make_addplot(j_line, panel=2, color="#a855f7", width=0.8, linestyle="--"))
            has_kdj_panel = True
    except Exception as exc:
        logger.debug("Chart overlay computation failed: %s", exc)

    mc = mpf.make_marketcolors(
        up="#16a34a", down="#dc2626",
        edge={"up": "#16a34a", "down": "#dc2626"},
        wick={"up": "#16a34a", "down": "#dc2626"},
        volume={"up": "#16a34a", "down": "#dc2626"},
    )
    style = mpf.make_mpf_style(marketcolors=mc, gridstyle=":", gridcolor="#e5e7eb")

    buf = io.BytesIO()
    try:
        figsize = (12, 8) if has_kdj_panel else (12, 7)
        plot_kwargs: dict[str, Any] = {
            "type": "candle",
            "volume": True,
            "style": style,
            "title": f"\n{symbol} K-Line ({bars}D)" if symbol else f"\nK-Line ({bars}D)",
            "savefig": {"fname": buf, "dpi": 120, "bbox_inches": "tight"},
            "mav": (5, 10, 20),
            "figsize": figsize,
        }
        if addplots:
            plot_kwargs["addplot"] = addplots
        mpf.plot(chart_df, **plot_kwargs)
        buf.seek(0)
        return base64.b64encode(buf.read()).decode("utf-8")
    except Exception as exc:
        logger.warning("Chart rendering failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Main analysis
# ---------------------------------------------------------------------------

def analyze_chart_pattern(
    stack: MarketStack,
    symbol: str,
    *,
    timeframe: str = "daily",
    bars: int = 40,
    multi_timeframe: bool = False,
    config: AppConfig | None = None,
    client: ChatClient | None = None,
) -> str:
    """Run visual pattern recognition on a symbol's K-line chart.

    Args:
        multi_timeframe: If True, also fetch weekly bars and include a second chart.

    Returns natural-language pattern report with indicator cross-validation.
    """
    app_config = config or stack.config
    try:
        chat_client = client or create_vision_client(app_config) or create_chat_client(app_config)
    except AgentUnavailableError as exc:
        return f"**视觉分析不可用:** {exc}"

    provider = stack.provider

    # --- Daily data ---
    try:
        raw_bars = provider.get_bars(symbol, timeframe="1d", limit=max(bars + 20, 80))
    except Exception as exc:
        return f"**数据获取失败:** {exc}"

    if not raw_bars or len(raw_bars) < 20:
        return f"**数据不足:** {symbol} 仅有 {len(raw_bars) if raw_bars else 0} 条K线"

    df = pd.DataFrame([
        {"open": b.open, "high": b.high, "low": b.low, "close": b.close, "volume": b.volume}
        for b in raw_bars
    ])

    indicators = _compute_indicators(df)
    indicator_text = _format_indicator_context(indicators, symbol)

    chart_b64 = render_kline_chart(df, symbol=symbol, bars=bars)
    if not chart_b64:
        return _fallback_text_analysis(df, symbol, chat_client, indicators)

    prompt_text = _PATTERN_PROMPT.format(
        timeframe=timeframe, symbol=symbol, bars=bars,
        indicator_context=indicator_text,
    )

    content_parts: list[dict[str, Any]] = [
        {"type": "text", "text": prompt_text},
        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{chart_b64}"}},
    ]

    # --- Weekly chart for multi-timeframe ---
    if multi_timeframe:
        weekly_chart = _render_weekly_chart(provider, symbol)
        if weekly_chart:
            content_parts.append({"type": "text", "text": "\n\n--- 以下是同一股票的**周线**图，用于多周期验证 ---"})
            content_parts.append({"type": "image_url", "image_url": {"url": f"data:image/png;base64,{weekly_chart}"}})

    messages = [ChatMessage(role="user", content=content_parts)]

    try:
        response = chat_client.complete(messages)
        report = response.content or "（分析无输出）"
    except Exception as exc:
        logger.warning("Vision LLM call failed: %s, falling back to text analysis", exc)
        return _fallback_text_analysis(df, symbol, chat_client, indicators)

    # Append structured indicator summary as JSON footer
    footer = "\n\n<details><summary>📊 量化指标原始数据</summary>\n\n```json\n"
    footer += json.dumps(indicators, ensure_ascii=False, indent=2)
    footer += "\n```\n</details>"

    return report + footer


def _render_weekly_chart(provider: Any, symbol: str) -> str | None:
    """Render a weekly candlestick chart for multi-timeframe confirmation.

    Tries native weekly bars first; if unavailable, aggregates from daily bars.
    """
    df: pd.DataFrame | None = None

    # Try native weekly first
    try:
        raw_bars = provider.get_bars(symbol, timeframe="1w", limit=52)
        if raw_bars and len(raw_bars) >= 10:
            df = pd.DataFrame([
                {"open": b.open, "high": b.high, "low": b.low, "close": b.close, "volume": b.volume}
                for b in raw_bars
            ])
    except Exception:
        pass

    # Fallback: aggregate daily bars into weekly
    if df is None or len(df) < 10:
        try:
            daily_bars = provider.get_bars(symbol, timeframe="1d", limit=260)
        except Exception:
            return None
        if not daily_bars or len(daily_bars) < 20:
            return None
        daily_df = pd.DataFrame([
            {"ts": b.timestamp, "open": b.open, "high": b.high, "low": b.low,
             "close": b.close, "volume": b.volume}
            for b in daily_bars
        ])
        daily_df["ts"] = pd.to_datetime(daily_df["ts"])
        daily_df.set_index("ts", inplace=True)
        weekly = daily_df.resample("W").agg({
            "open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum",
        }).dropna()
        if len(weekly) < 10:
            return None
        df = weekly.reset_index(drop=True)

    return render_kline_chart(df, symbol=f"{symbol} Weekly", bars=min(len(df), 52))


# ---------------------------------------------------------------------------
# Fallback
# ---------------------------------------------------------------------------

def _fallback_text_analysis(
    df: pd.DataFrame,
    symbol: str,
    client: ChatClient,
    indicators: dict[str, Any] | None = None,
) -> str:
    """Fallback: analyze from numeric data + pre-computed indicators when vision is unavailable."""
    if indicators is None:
        indicators = _compute_indicators(df)

    last_20 = df.tail(20)
    data_str = last_20.to_string(index=False)
    indicator_text = _format_indicator_context(indicators, symbol)

    messages = [
        ChatMessage(role="user", content=(
            f"以下是 {symbol} 最近20日的OHLCV数据:\n\n"
            f"{data_str}\n\n"
            f"{indicator_text}\n\n"
            "请综合以上量化指标和K线数据，分析：\n"
            "1. K线形态特征（是否有经典形态）\n"
            "2. 支撑阻力位\n"
            "3. 趋势方向和强度\n"
            "4. 量价关系\n"
            "5. 综合研判（方向、置信度、关键触发条件）"
        )),
    ]
    try:
        response = client.complete(messages)
        report = response.content or "（分析无输出）"
        footer = "\n\n<details><summary>📊 量化指标原始数据</summary>\n\n```json\n"
        footer += json.dumps(indicators, ensure_ascii=False, indent=2)
        footer += "\n```\n</details>"
        return report + footer
    except Exception as exc:
        return f"**分析失败:** {exc}"


# ---------------------------------------------------------------------------
# Agent-callable tool
# ---------------------------------------------------------------------------

def tool_chart_pattern(stack: MarketStack, **kwargs: Any) -> str:
    """Agent-callable tool for visual pattern analysis."""
    symbol = str(kwargs.get("symbol") or "")
    bars = int(kwargs.get("bars") or 40)
    multi_tf = bool(kwargs.get("multi_timeframe", False))

    if not symbol:
        return '{"error": "symbol is required"}'

    report = analyze_chart_pattern(stack, symbol, bars=bars, multi_timeframe=multi_tf)
    return report
