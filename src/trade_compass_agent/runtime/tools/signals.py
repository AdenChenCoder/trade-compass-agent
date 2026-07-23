"""Tool logic for emit_signal — records a structured TradingSignal."""

from __future__ import annotations

import json
from pathlib import Path

from trade_compass_agent.domain.signals import SignalRating, TradingSignal
from trade_compass_agent.evaluation.signal_tracker import SignalTracker
from trade_compass_agent.ops.audit import JsonAuditLog
from trade_compass_agent.runtime.market_stack import MarketStack


def tool_emit_signal(stack: MarketStack, **kwargs: object) -> str:
    """Validate, persist, and audit a trading signal.

    The agent calls this tool after completing analysis to formally record
    a directional view. The signal is written to both signals.jsonl (for
    tracking) and the audit log (for traceability).
    """
    symbol = str(kwargs.get("symbol") or "")
    rating_raw = str(kwargs.get("rating") or "hold")
    confidence_raw = kwargs.get("confidence")
    if confidence_raw is None:
        confidence_raw = 0.5
    entry_price = kwargs.get("entry_price")
    stop_loss = kwargs.get("stop_loss")
    target_price = kwargs.get("target_price")
    reasoning = str(kwargs.get("reasoning", ""))
    source_specialist = str(kwargs.get("source_specialist", "agent"))
    source_tools_raw = kwargs.get("source_tools")
    source_skills_raw = kwargs.get("source_skills")

    if not symbol:
        return json.dumps({"error": "symbol is required"}, ensure_ascii=False)
    if not reasoning:
        return json.dumps({"error": "reasoning is required"}, ensure_ascii=False)

    try:
        rating = SignalRating(rating_raw)
    except ValueError:
        return json.dumps(
            {"error": f"invalid rating '{rating_raw}', must be one of: {[r.value for r in SignalRating]}"},
            ensure_ascii=False,
        )

    confidence = max(0.0, min(1.0, float(confidence_raw)))

    source_tools: list[str] = []
    if isinstance(source_tools_raw, list):
        source_tools = [str(t) for t in source_tools_raw]
    elif isinstance(source_tools_raw, str):
        source_tools = [s.strip() for s in source_tools_raw.split(",") if s.strip()]

    source_skills: list[str] = []
    if isinstance(source_skills_raw, list):
        source_skills = [str(t) for t in source_skills_raw]
    elif isinstance(source_skills_raw, str):
        source_skills = [s.strip() for s in source_skills_raw.split(",") if s.strip()]

    rr_ratio: float | None = None
    entry_f = float(entry_price) if entry_price is not None else None
    stop_f = float(stop_loss) if stop_loss is not None else None
    target_f = float(target_price) if target_price is not None else None

    if entry_f and stop_f and target_f and entry_f != stop_f:
        rr_ratio = round((target_f - entry_f) / (entry_f - stop_f), 2)

    signal = TradingSignal(
        symbol=symbol,
        rating=rating,
        confidence=confidence,
        entry_price=entry_f,
        stop_loss=stop_f,
        target_price=target_f,
        risk_reward_ratio=rr_ratio,
        reasoning=reasoning,
        source_specialist=source_specialist,
        source_tools=source_tools,
        source_skills=source_skills,
    )

    _persist_signal(stack.config.data_dir, signal)
    _record_audit(stack.config.data_dir, signal)
    _track_signal(stack.config.data_dir, signal)

    return json.dumps(
        {
            "status": "recorded",
            "signal_id": signal.signal_id,
            "symbol": signal.symbol,
            "rating": signal.rating.value,
            "confidence": signal.confidence,
            "entry_price": signal.entry_price,
            "stop_loss": signal.stop_loss,
            "target_price": signal.target_price,
            "risk_reward_ratio": signal.risk_reward_ratio,
            "source_skills": signal.source_skills,
        },
        ensure_ascii=False,
    )


def _persist_signal(data_dir: Path, signal: TradingSignal) -> None:
    """Append signal to signals.jsonl."""
    signals_path = data_dir / "signals.jsonl"
    signals_path.parent.mkdir(parents=True, exist_ok=True)
    with signals_path.open("a", encoding="utf-8") as f:
        f.write(signal.model_dump_json() + "\n")


def _record_audit(data_dir: Path, signal: TradingSignal) -> None:
    """Mirror signal to the audit log."""
    audit = JsonAuditLog(data_dir / "audit.jsonl")
    audit.record(
        event_type="trading_signal",
        summary=f"{signal.symbol} {signal.rating.value} (conf={signal.confidence:.0%})",
        payload=signal.model_dump(),
    )


def _track_signal(data_dir: Path, signal: TradingSignal) -> None:
    """Register signal in the tracking system for lifecycle monitoring."""
    tracker = SignalTracker(data_dir)
    tracker.track_signal(signal.model_dump())
