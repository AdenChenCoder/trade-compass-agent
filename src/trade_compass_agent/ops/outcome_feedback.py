"""Outcome-based disproof feedback for KNOWLEDGE confidence governance."""

from __future__ import annotations

import logging
import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from trade_compass_agent.config import AppConfig, MemoryGovernanceConfig
from trade_compass_agent.memory.memory_store import MemoryStore, _content_hash
from trade_compass_agent.ops.reflection import PendingReflection
from trade_compass_agent.ops.reflection_resolver import extract_alerts

logger = logging.getLogger(__name__)

OUTCOME_ADVISOR_SYSTEM = """You are an outcome-feedback advisor.
Your only job is to propose structured outcome candidates for a deterministic gate.
You cannot change memory, confidence, or scores.
Return JSON only:
{"outcomes":[{"type":"missed_upside","target":"...","symbol":"...","actual_return_pct":0.0,"expected_return_pct":0.0,"threshold_pct":5.0,"explanation":"..."}]}
Only include candidates supported by the given predictions, actuals, and lesson.
If uncertain, return {"outcomes":[]}.
"""


@dataclass(frozen=True)
class ImplicatedEntry:
    entry: dict[str, Any]
    match_reason: str


@dataclass(frozen=True)
class OutcomeFeedbackReport:
    job_id: str
    run_id: str
    run_date: str
    signals: list[dict[str, Any]]
    match_reason: str
    entry_hash: str
    entry_text: str
    delta: float
    explanation: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "run_id": self.run_id,
            "run_date": self.run_date,
            "signals": self.signals,
            "match_reason": self.match_reason,
            "entry_hash": self.entry_hash,
            "entry_text": self.entry_text,
            "delta": self.delta,
            "explanation": self.explanation,
        }


def _gov(config: AppConfig) -> MemoryGovernanceConfig:
    return config.memory.governance


def advise_outcome_candidates(
    pending: PendingReflection,
    actuals: dict[str, Any],
    lesson: str,
    llm_call: Any,
    *,
    max_candidates: int = 5,
) -> dict[str, Any]:
    """Ask an LLM/agent for structured outcome candidates without applying feedback."""
    if llm_call is None:
        return {"outcomes": []}

    user_content = json.dumps(
        {
            "job_id": pending.job_id,
            "run_id": pending.run_id,
            "run_date": pending.run_date,
            "predictions": pending.predictions,
            "actuals": actuals,
            "lesson": lesson,
            "allowed_types": ["missed_upside", "avoid_wrong", "thesis_invalidated", "scope_wrong"],
            "max_candidates": max_candidates,
        },
        ensure_ascii=False,
        default=str,
    )
    try:
        raw = llm_call(OUTCOME_ADVISOR_SYSTEM, user_content)
    except Exception as exc:
        logger.debug("Outcome advisor failed: %s", exc)
        return {"outcomes": []}

    candidates = _parse_advisor_response(raw)
    if not candidates:
        return {"outcomes": []}
    return {"outcomes": _sanitize_advisor_outcomes(candidates, max_candidates=max_candidates)}


def enrich_actuals_with_outcome_advisor(
    pending: PendingReflection,
    actuals: dict[str, Any],
    lesson: str,
    llm_call: Any,
    *,
    max_candidates: int = 5,
) -> dict[str, Any]:
    """Merge advisor candidates into actuals without mutating the caller's dict."""
    advised = advise_outcome_candidates(
        pending,
        actuals,
        lesson,
        llm_call,
        max_candidates=max_candidates,
    )
    if not advised.get("outcomes"):
        return actuals
    merged = dict(actuals)
    merged["outcomes"] = list(actuals.get("outcomes") or []) + list(advised["outcomes"])
    return merged


def _parse_advisor_response(raw: str | None) -> list[dict[str, Any]]:
    if not raw:
        return []
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:].strip()
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        text = text[start : end + 1]
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        logger.debug("Outcome advisor returned invalid JSON")
        return []
    outcomes = parsed.get("outcomes") if isinstance(parsed, dict) else parsed
    if not isinstance(outcomes, list):
        return []
    return [item for item in outcomes if isinstance(item, dict)]


def _sanitize_advisor_outcomes(
    outcomes: list[dict[str, Any]],
    *,
    max_candidates: int,
) -> list[dict[str, Any]]:
    allowed = {"missed_upside", "avoid_wrong", "thesis_invalidated", "scope_wrong"}
    limit = max(0, max_candidates)
    if limit == 0:
        return []
    sanitized: list[dict[str, Any]] = []
    for item in outcomes:
        signal = str(item.get("type") or item.get("signal") or "").lower()
        target = str(item.get("target") or item.get("symbol") or item.get("scope") or "").strip()
        if signal not in allowed or not target:
            continue
        row: dict[str, Any] = {
            "type": signal,
            "target": target,
            "symbol": str(item.get("symbol") or ""),
            "advisor_source": "llm",
        }
        for key in ("actual_return_pct", "expected_return_pct", "threshold_pct", "confidence"):
            value = _maybe_float(item.get(key))
            if value is not None:
                row[key] = value
        explanation = str(item.get("explanation") or item.get("reason") or "").strip()
        if explanation:
            row["advisor_explanation"] = explanation[:500]
        if signal == "thesis_invalidated":
            row["correct"] = False
        sanitized.append(row)
        if len(sanitized) >= limit:
            break
    return sanitized


def parse_alert_symbols(alerts: list[str]) -> set[str]:
    symbols: set[str] = set()
    for alert in alerts:
        tok = alert.split()[0] if alert else ""
        if len(tok) == 6 and tok.isdigit():
            symbols.add(tok)
    return symbols


def _signal_pnl_deviation(pos: dict[str, Any], threshold: float) -> bool:
    return abs(float(pos.get("delta_pnl_pct", 0))) >= threshold


def _signal_direction_wrong(pos: dict[str, Any], min_predicted: float) -> bool:
    predicted = float(pos.get("predicted_pnl_pct", 0))
    actual = float(pos.get("actual_pnl_pct", 0))
    if abs(predicted) < min_predicted:
        return False
    return predicted > 0 and actual < 0


def _signal_alert_not_heeded(
    pos: dict[str, Any],
    alerted_symbols: set[str],
    drop_threshold: float,
) -> bool:
    symbol = pos.get("symbol")
    if not symbol or symbol not in alerted_symbols:
        return False
    if pos.get("status") == "closed":
        return False
    predicted = float(pos.get("predicted_pnl_pct", 0))
    actual = float(pos.get("actual_pnl_pct", 0))
    if predicted <= 10:
        return False
    return actual < predicted - drop_threshold or actual < 0


def _reason(symbol_value: str, signal: str, severity: float, **fields: Any) -> dict[str, Any]:
    return {
        **fields,
        "symbol": symbol_value,
        "target": fields.get("target") or symbol_value,
        "signal": signal,
        "severity": max(0.1, float(severity)),
    }


def is_disproven(
    pending: PendingReflection,
    actuals: dict[str, Any],
    config: AppConfig,
) -> tuple[bool, list[dict[str, Any]]]:
    """Return whether outcome disproves prior context and structured reasons."""
    gov = _gov(config)
    if not gov.outcome_feedback_enabled:
        return False, []

    reasons: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    alerted = parse_alert_symbols(extract_alerts(pending.predictions))

    for pos in actuals.get("positions", []):
        symbol = pos.get("symbol", "")
        delta_pnl = float(pos.get("delta_pnl_pct", 0))
        if _signal_pnl_deviation(pos, gov.disproof_pnl_delta):
            key = (symbol, "pnl_deviation")
            if key not in seen:
                seen.add(key)
                reasons.append(
                    _reason(
                        symbol,
                        "pnl_deviation",
                        _severity_from_delta(abs(delta_pnl), gov.disproof_pnl_delta),
                        **pos,
                    )
                )
        if _signal_direction_wrong(pos, gov.min_predicted_magnitude):
            key = (symbol, "direction_wrong")
            if key not in seen:
                seen.add(key)
                reasons.append(_reason(symbol, "direction_wrong", 1.5, **pos))
        if gov.alert_signal_enabled and _signal_alert_not_heeded(
            pos, alerted, gov.alert_drop_threshold
        ):
            key = (symbol, "alert_not_heeded")
            if key not in seen:
                seen.add(key)
                reasons.append(_reason(symbol, "alert_not_heeded", 1.3, **pos))

    for outcome in actuals.get("outcomes", []):
        for reason in _structured_outcome_reasons(outcome, gov):
            key = (reason.get("target", ""), reason.get("signal", ""))
            if key not in seen:
                seen.add(key)
                reasons.append(reason)

    return bool(reasons), reasons


def _structured_outcome_reasons(
    outcome: dict[str, Any],
    gov: MemoryGovernanceConfig,
) -> list[dict[str, Any]]:
    """Evaluate structured non-position outcomes without parsing free-form text."""
    signal = str(outcome.get("signal") or outcome.get("type") or "").lower()
    action = str(outcome.get("recommendation") or outcome.get("action") or "").lower()
    target = str(outcome.get("target") or outcome.get("symbol") or outcome.get("scope") or "").strip()
    symbol = str(outcome.get("symbol") or "")
    actual_return = _maybe_float(outcome.get("actual_return_pct"))
    expected_return = _maybe_float(outcome.get("expected_return_pct"))
    threshold = _maybe_float(outcome.get("threshold_pct")) or gov.outcome_return_delta

    if signal in {"missed_upside", "recommendation_missed"} or (
        action in {"watch", "buy", "关注", "买入"} and outcome.get("executed") is False
    ):
        if actual_return is not None and actual_return >= threshold:
            fields = {**outcome, "target": target or symbol, "actual_return_pct": actual_return}
            return [
                _reason(
                    symbol,
                    "missed_upside",
                    _severity_from_delta(actual_return, threshold),
                    **fields,
                )
            ]

    if signal in {"avoid_wrong", "avoid_but_up"} or action in {"avoid", "skip", "回避"}:
        if actual_return is not None and actual_return >= threshold:
            fields = {**outcome, "target": target or symbol, "actual_return_pct": actual_return}
            return [
                _reason(
                    symbol,
                    "avoid_wrong",
                    _severity_from_delta(actual_return, threshold),
                    **fields,
                )
            ]

    if signal in {"thesis_invalidated", "strategy_wrong"} or outcome.get("correct") is False:
        confidence = _maybe_float(outcome.get("confidence")) or 1.0
        fields = {**outcome, "target": target or symbol}
        return [
            _reason(
                symbol,
                "thesis_invalidated",
                max(1.0, confidence),
                **fields,
            )
        ]

    if signal in {"scope_wrong", "sector_wrong", "index_wrong", "macro_wrong"}:
        if expected_return is None or actual_return is None:
            return []
        miss = abs(actual_return - expected_return)
        if miss >= threshold:
            fields = {
                **outcome,
                "target": target or symbol,
                "actual_return_pct": actual_return,
                "expected_return_pct": expected_return,
            }
            return [
                _reason(
                    symbol,
                    "scope_wrong",
                    _severity_from_delta(miss, threshold),
                    **fields,
                )
            ]

    return []


def _maybe_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _severity_from_delta(value: float, threshold: float) -> float:
    if threshold <= 0:
        return 1.0
    ratio = value / threshold
    if ratio >= 3.0:
        return 2.0
    if ratio >= 2.0:
        return 1.5
    if ratio >= 1.0:
        return 1.0
    return 0.5


def find_implicated_entries(
    mem_store: MemoryStore,
    pending: PendingReflection,
    symbols: set[str],
    *,
    job_window_days: int = 7,
    min_confidence: float = 0.5,
    legacy_symbol_fallback: bool = False,
) -> list[dict[str, Any]]:
    """Locate Active KNOWLEDGE meta rows implicated by outcome disproof."""
    return [
        match.entry
        for match in find_implicated_entry_matches(
            mem_store,
            pending,
            symbols,
            job_window_days=job_window_days,
            min_confidence=min_confidence,
            legacy_symbol_fallback=legacy_symbol_fallback,
        )
    ]


def find_implicated_entry_matches(
    mem_store: MemoryStore,
    pending: PendingReflection,
    symbols: set[str],
    *,
    job_window_days: int = 7,
    min_confidence: float = 0.5,
    legacy_symbol_fallback: bool = False,
) -> list[ImplicatedEntry]:
    """Locate implicated entries and record why each entry was selected."""
    metas = mem_store.get_active_meta("memory", min_confidence=min_confidence)
    if not metas:
        return []

    candidates = [m for m in metas if m.get("source") != "user_pin"]

    by_run = [
        ImplicatedEntry(m, "promoted_by_run_id")
        for m in candidates
        if pending.run_id and m.get("promoted_by_run_id") == pending.run_id
    ]
    if by_run:
        return by_run

    obs_ids = _extract_obs_ids(pending.predictions)
    if obs_ids:
        by_obs = [
            ImplicatedEntry(m, "source_obs_ids")
            for m in candidates
            if obs_ids & set(m.get("source_obs_ids") or [])
        ]
        if by_obs:
            return by_obs

    try:
        run_day = date.fromisoformat(pending.run_date)
    except ValueError:
        run_day = None

    if run_day is not None:
        window_start = run_day - timedelta(days=job_window_days)
        window_end = run_day + timedelta(days=job_window_days)
        by_job_symbol_window = [
            ImplicatedEntry(m, "promoted_by_job_id+symbol+window")
            for m in candidates
            if m.get("source") == "promotion"
            and m.get("promoted_by_job_id") == pending.job_id
            and _promoted_in_window(m.get("promoted_at", ""), window_start, window_end)
            and _matches_any_symbol(m, symbols)
        ]
        if by_job_symbol_window:
            return by_job_symbol_window

    if legacy_symbol_fallback:
        by_symbol = [
            ImplicatedEntry(m, "legacy_symbol")
            for m in candidates
            if m.get("source") == "promotion" and _matches_any_symbol(m, symbols)
        ]
        if len(by_symbol) == 1:
            return by_symbol

    return []


def _extract_obs_ids(value: Any) -> set[str]:
    """Best-effort extraction for structured prediction provenance fields."""
    if isinstance(value, dict):
        found: set[str] = set()
        for key, nested in value.items():
            if key in {"source_obs_id", "observation_id", "obs_id"}:
                if isinstance(nested, str) and nested:
                    found.add(nested)
            elif key in {"source_obs_ids", "observation_ids", "obs_ids"}:
                if isinstance(nested, list):
                    found |= {item for item in nested if isinstance(item, str) and item}
            else:
                found |= _extract_obs_ids(nested)
        return found
    if isinstance(value, list):
        found: set[str] = set()
        for item in value:
            found |= _extract_obs_ids(item)
        return found
    return set()


def _matches_any_symbol(entry: dict[str, Any], symbols: set[str]) -> bool:
    text = entry.get("text", "")
    return bool(symbols) and any(sym in text for sym in symbols)


def _reason_targets(reasons: list[dict[str, Any]]) -> set[str]:
    targets: set[str] = set()
    for reason in reasons:
        for key in ("symbol", "target", "scope", "scope_id", "topic"):
            value = reason.get(key)
            if isinstance(value, str) and value.strip():
                targets.add(value.strip())
    return targets


def _confidence_delta_for_reasons(
    reasons: list[dict[str, Any]],
    gov: MemoryGovernanceConfig,
) -> float:
    if not reasons:
        return 0.0
    base = abs(gov.outcome_confidence_delta)
    severity = max(float(r.get("severity") or 1.0) for r in reasons)
    independent_targets = {r.get("target") or r.get("symbol") for r in reasons if r.get("target") or r.get("symbol")}
    if len(independent_targets) >= 2:
        severity += 0.5
    raw = -(base * severity)
    lower = min(gov.outcome_min_confidence_delta, gov.outcome_max_confidence_delta)
    upper = max(gov.outcome_min_confidence_delta, gov.outcome_max_confidence_delta)
    return max(lower, min(upper, raw))


def _build_report(
    pending: PendingReflection,
    match: ImplicatedEntry,
    reasons: list[dict[str, Any]],
    entry_hash: str,
    delta: float,
) -> OutcomeFeedbackReport:
    signals = sorted({r.get("signal") or "unknown" for r in reasons})
    targets = sorted(_reason_targets(reasons))
    explanation = (
        f"Outcome feedback matched by {match.match_reason}; "
        f"signals={','.join(signals)}; targets={','.join(targets) or 'n/a'}; delta={delta:.2f}"
    )
    return OutcomeFeedbackReport(
        job_id=pending.job_id,
        run_id=pending.run_id,
        run_date=pending.run_date,
        signals=reasons,
        match_reason=match.match_reason,
        entry_hash=entry_hash,
        entry_text=match.entry.get("text", ""),
        delta=delta,
        explanation=explanation,
    )


def _promoted_in_window(promoted_at: str, start: date, end: date) -> bool:
    if not promoted_at:
        return False
    try:
        promoted_day = datetime.fromisoformat(promoted_at.replace("Z", "+00:00")).date()
    except ValueError:
        return False
    return start <= promoted_day <= end


def apply_outcome_feedback(
    pending: PendingReflection,
    actuals: dict[str, Any],
    lesson: str,
    mem_store: MemoryStore,
    config: AppConfig,
) -> list[dict[str, Any]]:
    """Adjust KNOWLEDGE confidence when market outcome disproves prior context."""
    disproven, reasons = is_disproven(pending, actuals, config)
    if not disproven:
        return []

    symbols = _reason_targets(reasons)
    gov = _gov(config)
    matches = find_implicated_entry_matches(
        mem_store,
        pending,
        symbols,
        min_confidence=gov.min_inject_confidence,
        legacy_symbol_fallback=gov.legacy_promotion_fallback,
    )
    if not matches:
        logger.debug(
            "Outcome disproven for %s/%s but no implicated KNOWLEDGE entries",
            pending.job_id,
            pending.run_date,
        )
        return []

    results: list[dict[str, Any]] = []
    primary_signal = reasons[0].get("signal") or "unknown"

    for match in matches:
        entry = match.entry
        entry_hash = entry.get("content_hash") or entry.get("dedup_hash") or _content_hash(entry.get("text", ""))
        delta = _confidence_delta_for_reasons(reasons, gov)
        reason = f"outcome:{primary_signal}:{match.match_reason}:{pending.job_id}:{pending.run_date}"
        report = _build_report(pending, match, reasons, entry_hash, delta)
        result = mem_store.adjust_confidence(
            entry_hash=entry_hash,
            delta=delta,
            reason=reason,
            run_id=pending.run_id,
            archive_after_disproofs=gov.archive_after_disproofs,
        )
        if result.get("ok"):
            result["match_reason"] = match.match_reason
            result["outcome_reason"] = reason
            result["outcome_signals"] = sorted({r.get("signal") or "unknown" for r in reasons})
            result["implicated_symbols"] = sorted(symbols)
            result["implicated_targets"] = sorted(symbols)
            result["entry_text"] = entry.get("text", "")
            result["delta"] = delta
            result["outcome_report"] = report.as_dict()
            result["explanation"] = report.explanation
            results.append(result)
            logger.info(
                "Outcome feedback: %s confidence %.2f → %.2f (%s via %s)",
                entry_hash[:8],
                result.get("previous_confidence"),
                result.get("confidence"),
                primary_signal,
                match.match_reason,
            )

    return results
