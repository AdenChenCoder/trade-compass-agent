"""Structured output parsers for specialist reports."""

from __future__ import annotations

import re
import json
from typing import Any

from trade_compass_agent.domain.signals import SignalRating, TradingSignal, parse_signal_rating


def parse_screener_signals(report: str, candidates: list[str]) -> list[TradingSignal]:
    """Parse screener JSON/markdown into trading signal domain objects."""
    json_signals = _parse_json_signals(report, candidates)
    if json_signals:
        return json_signals
    markdown_signals = _parse_markdown_sections(report, candidates)
    if markdown_signals:
        return markdown_signals
    return _parse_markdown_table(report, candidates)


def _parse_json_signals(report: str, candidates: list[str]) -> list[TradingSignal]:
    parsed = _json_object(report)
    if not parsed:
        return []
    raw_signals = parsed.get("signals")
    if not isinstance(raw_signals, list):
        return []
    signals: list[TradingSignal] = []
    for item in raw_signals:
        if not isinstance(item, dict):
            continue
        symbol = str(item.get("symbol") or "").strip()
        if symbol not in candidates:
            continue
        signals.append(_signal_from_mapping(symbol, item))
    return signals


def _json_object(report: str) -> dict[str, Any] | None:
    text = report.strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.DOTALL)
        if not match:
            return None
        try:
            parsed = json.loads(match.group(1))
        except json.JSONDecodeError:
            return None
    return parsed if isinstance(parsed, dict) else None


def _parse_markdown_sections(report: str, candidates: list[str]) -> list[TradingSignal]:
    signals: list[TradingSignal] = []
    heading = re.compile(r"^#{2,4}\s+(?:\d+[.)]\s*)?\**(\d{6})\**[^\n]*$", re.MULTILINE)
    matches = list(heading.finditer(report))

    for index, match in enumerate(matches):
        symbol = match.group(1).strip()
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(report)
        body = report[start:end]

        if symbol not in candidates:
            continue

        rating = _extract_field(body, r"\*\*Rating\*\*:\s*(\S+)")
        confidence_str = _extract_field(body, r"\*\*Confidence\*\*:\s*([\d.]+)")
        entry_str = _extract_field(body, r"\*\*Entry Price\*\*:\s*([\d.]+)")
        stop_str = _extract_field(body, r"\*\*Stop Loss\*\*:\s*([\d.]+)")
        target_str = _extract_field(body, r"\*\*Target Price\*\*:\s*([\d.]+)")
        reasoning = _extract_field(
            body,
            r"\*\*Reasoning\*\*:\s*(.+?)(?:\n\*\*|\Z)",
            dotall=True,
        )
        signals.append(_signal_from_mapping(
            symbol,
            {
                "rating": rating or parse_signal_rating(body).value,
                "confidence": confidence_str,
                "entry_price": entry_str,
                "stop_loss": stop_str,
                "target_price": target_str,
                "reasoning": reasoning or "（AI 分析摘要见上文）",
            },
        ))

    return signals


def _parse_markdown_table(report: str, candidates: list[str]) -> list[TradingSignal]:
    signals: list[TradingSignal] = []
    for line in report.splitlines():
        if "|" not in line:
            continue
        cells = [re.sub(r"[*`]", "", cell).strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) < 3:
            continue
        symbol_match = re.search(r"\b(\d{6})\b", cells[0])
        if not symbol_match:
            continue
        symbol = symbol_match.group(1)
        if symbol not in candidates:
            continue
        rating = next((cell for cell in cells[1:] if _looks_like_rating(cell)), "")
        confidence = next((cell for cell in cells[1:] if re.search(r"\b(?:0?\.\d+|1(?:\.0+)?)\b", cell)), "")
        reasoning = cells[-1] if cells[-1] else "（AI 表格摘要见上文）"
        signals.append(_signal_from_mapping(
            symbol,
            {
                "rating": rating,
                "confidence": confidence,
                "reasoning": reasoning,
            },
        ))
    return signals


def _signal_from_mapping(symbol: str, raw: dict[str, Any]) -> TradingSignal:
    rating_raw = str(raw.get("rating") or raw.get("Rating") or "").lower().strip()
    try:
        rating = SignalRating(rating_raw)
    except ValueError:
        rating = parse_signal_rating(rating_raw)
    entry = _optional_float(raw.get("entry_price") or raw.get("entry") or raw.get("Entry Price"))
    stop = _optional_float(raw.get("stop_loss") or raw.get("stop") or raw.get("Stop Loss"))
    target = _optional_float(raw.get("target_price") or raw.get("target") or raw.get("Target Price"))
    rr = _optional_float(raw.get("risk_reward_ratio"))
    if rr is None and entry and stop and target and entry != stop:
        rr = round((target - entry) / (entry - stop), 2)
    reasoning = str(raw.get("reasoning") or raw.get("rationale") or raw.get("reason") or "（AI 分析摘要见上文）")
    return TradingSignal(
        symbol=symbol,
        rating=rating,
        confidence=max(0.0, min(1.0, _optional_float(raw.get("confidence") or raw.get("Confidence")) or 0.5)),
        entry_price=entry,
        stop_loss=stop,
        target_price=target,
        risk_reward_ratio=rr,
        reasoning=reasoning.strip(),
        source_specialist="screener",
        source_tools=["get_bars", "compute_ma", "compute_rsi"],
    )


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.upper() in {"N/A", "NA", "NONE", "NULL", "-"}:
        return None
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def _looks_like_rating(value: str) -> bool:
    text = value.lower().strip()
    return text in {item.value for item in SignalRating}


def _extract_field(text: str, pattern: str, dotall: bool = False) -> str | None:
    flags = re.DOTALL if dotall else 0
    match = re.search(pattern, text, flags)
    return match.group(1).strip() if match else None
