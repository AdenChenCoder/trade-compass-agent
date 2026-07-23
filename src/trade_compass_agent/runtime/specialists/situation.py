"""Situation Summariser — aggregate multi-source context into actionable brief.

Pulls together:
- Market pulse (sector strength, limit-up)
- Portfolio status (positions, P&L)
- Recent signals
- Hot sectors/concepts

Provides the debate specialists with a shared situational context.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from trade_compass_agent.runtime.market_stack import MarketStack
from trade_compass_agent.runtime.tools.market import tool_get_market_pulse
from trade_compass_agent.runtime.tools.portfolio import tool_analyze_portfolio

logger = logging.getLogger(__name__)


def build_situation_summary(stack: MarketStack) -> str:
    """Build a comprehensive situation summary for specialist context.

    Returns a markdown-formatted brief covering market state, portfolio, and signals.
    """
    sections: list[str] = []

    # Market pulse
    try:
        pulse_raw = tool_get_market_pulse(stack)
        pulse = json.loads(pulse_raw)
        sections.append(_format_market_pulse(pulse))
    except Exception as exc:
        sections.append(f"## 市场概况\n数据获取失败: {exc}")

    # Portfolio overview
    try:
        portfolio_raw = tool_analyze_portfolio(stack)
        portfolio = json.loads(portfolio_raw)
        sections.append(_format_portfolio_brief(portfolio))
    except Exception as exc:
        sections.append(f"## 持仓概况\n数据获取失败: {exc}")

    # Recent signals
    try:
        signals = _load_recent_signals(stack)
        if signals:
            sections.append(_format_recent_signals(signals))
    except Exception:
        pass

    return "\n\n".join(sections)


def _format_market_pulse(pulse: dict[str, Any]) -> str:
    """Format market pulse into brief."""
    lines = ["## 市场概况"]
    if "error" in pulse:
        lines.append(f"- 数据状态: {pulse['error']}")
        return "\n".join(lines)

    if "sectors" in pulse:
        sectors = pulse["sectors"]
        if isinstance(sectors, list):
            top3 = sectors[:3]
            lines.append("- 领涨板块: " + ", ".join(
                f"{s.get('name', '?')}({s.get('change_pct', 0):.1f}%)" for s in top3
            ))

    if "limit_up_count" in pulse:
        lines.append(f"- 涨停: {pulse['limit_up_count']}, 跌停: {pulse.get('limit_down_count', 0)}")

    return "\n".join(lines)


def _format_portfolio_brief(portfolio: dict[str, Any]) -> str:
    """Format portfolio into brief."""
    lines = ["## 持仓概况"]
    total = portfolio.get("total_positions", 0)
    value = portfolio.get("total_market_value", 0)
    lines.append(f"- 持仓数: {total}, 总市值: {value:.0f}")

    positions = portfolio.get("positions", [])
    for p in positions[:5]:
        pnl_pct = p.get("pnl_pct", 0)
        lines.append(f"- {p['symbol']}: {pnl_pct:+.1f}%")

    return "\n".join(lines)


def _format_recent_signals(signals: list[dict[str, Any]]) -> str:
    """Format recent signals into brief."""
    lines = ["## 近期信号"]
    for s in signals[-5:]:
        lines.append(f"- {s.get('symbol', '?')} {s.get('rating', '?')} conf={s.get('confidence', 0):.1f}")
    return "\n".join(lines)


def _load_recent_signals(stack: MarketStack) -> list[dict[str, Any]]:
    """Load recent signals from signals.jsonl."""
    path = stack.config.data_dir / "signals.jsonl"
    if not path.exists():
        return []
    signals: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines()[-10:]:
        if line.strip():
            try:
                signals.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return signals
