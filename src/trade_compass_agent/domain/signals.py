"""Structured trading signal schema for agent output.

Defines the structured signal contract shared by screening and evaluation:
- 5-tier rating (adapted to A-share context)
- Pydantic schema with field descriptions as model instructions
- render helper for markdown wire format
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional
from uuid import uuid4

from pydantic import BaseModel, Field


class SignalRating(str, Enum):
    """5-tier directional rating for a trading signal."""

    STRONG_BUY = "strong_buy"
    BUY = "buy"
    HOLD = "hold"
    SELL = "sell"
    STRONG_SELL = "strong_sell"


class TradingSignal(BaseModel):
    """Structured signal emitted after agent analysis.

    Each signal gets a unique ID, is persisted to signals.jsonl,
    and mirrored to the audit log for traceability.
    """

    signal_id: str = Field(
        default_factory=lambda: str(uuid4()),
        description="Unique identifier for this signal.",
    )
    symbol: str = Field(
        description="Stock code, e.g. '600519'.",
    )
    rating: SignalRating = Field(
        description=(
            "Directional view. Exactly one of: strong_buy / buy / hold / sell / strong_sell. "
            "Reserve hold for genuinely balanced evidence."
        ),
    )
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="Confidence in the rating, 0.0 to 1.0.",
    )
    entry_price: Optional[float] = Field(
        default=None,
        description="Suggested entry price. Required for buy/strong_buy.",
    )
    stop_loss: Optional[float] = Field(
        default=None,
        description="Stop-loss price level.",
    )
    target_price: Optional[float] = Field(
        default=None,
        description="Target/take-profit price level.",
    )
    risk_reward_ratio: Optional[float] = Field(
        default=None,
        description="Risk-reward ratio = (target - entry) / (entry - stop_loss).",
    )
    reasoning: str = Field(
        description="2-4 sentence justification anchored in tool results.",
    )
    source_specialist: str = Field(
        default="agent",
        description="Which specialist produced this signal.",
    )
    source_tools: list[str] = Field(
        default_factory=list,
        description="Tools used to derive this signal.",
    )
    source_skills: list[str] = Field(
        default_factory=list,
        description="Skills that influenced this signal (for performance attribution).",
    )
    timestamp: str = Field(
        default_factory=lambda: datetime.now().isoformat(timespec="seconds"),
        description="ISO timestamp of signal creation.",
    )


def render_trading_signal(signal: TradingSignal) -> str:
    """Render a TradingSignal to markdown for display and memory."""
    rating_zh = {
        "strong_buy": "强烈看多",
        "buy": "看多",
        "hold": "观望",
        "sell": "看空",
        "strong_sell": "强烈看空",
    }
    parts = [
        f"**信号**: {signal.symbol} — {rating_zh.get(signal.rating.value, signal.rating.value)}",
        f"**置信度**: {signal.confidence:.0%}",
    ]
    if signal.entry_price is not None:
        parts.append(f"**入场价**: {signal.entry_price}")
    if signal.stop_loss is not None:
        parts.append(f"**止损价**: {signal.stop_loss}")
    if signal.target_price is not None:
        parts.append(f"**目标价**: {signal.target_price}")
    if signal.risk_reward_ratio is not None:
        parts.append(f"**风险收益比**: {signal.risk_reward_ratio:.2f}")
    parts.append(f"**理由**: {signal.reasoning}")
    parts.append(f"**来源**: {signal.source_specialist} | ID: {signal.signal_id[:8]}")
    return "\n".join(parts)


def parse_signal_rating(text: str) -> SignalRating:
    """Extract a SignalRating from free-text output (regex fallback).

    Deterministic extraction keeps rating parsing stable across model providers
    without an extra LLM call.
    """
    import re

    text_lower = text.lower()

    for line in text.splitlines():
        m = re.search(r"rating.*?[:\-]\s*\**\s*(\w+)", line, re.IGNORECASE)
        if m:
            word = m.group(1).lower()
            for member in SignalRating:
                if member.value == word:
                    return member

    priority = [
        SignalRating.STRONG_BUY,
        SignalRating.STRONG_SELL,
        SignalRating.BUY,
        SignalRating.SELL,
        SignalRating.HOLD,
    ]
    for member in priority:
        if member.value in text_lower:
            return member

    return SignalRating.HOLD
