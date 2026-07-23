from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from statistics import mean
from typing import Any

from trade_compass_agent.data import MarketDataProvider
from trade_compass_agent.domain import Bar


@dataclass(frozen=True)
class WorkflowEvaluationReport:
    as_of: str
    lookback_days: int
    catalyst_metrics: dict[str, Any] = field(default_factory=dict)
    idea_metrics: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def model_dump(self) -> dict[str, Any]:
        return {
            "as_of": self.as_of,
            "lookback_days": self.lookback_days,
            "catalyst_metrics": self.catalyst_metrics,
            "idea_metrics": self.idea_metrics,
            "warnings": self.warnings,
        }


class WorkflowArtifactEvaluator:
    def __init__(self, data_dir: Path, provider: MarketDataProvider | None = None) -> None:
        self.data_dir = data_dir
        self.provider = provider

    def evaluate(self, *, as_of: date, lookback_days: int = 7) -> WorkflowEvaluationReport:
        days = [as_of - timedelta(days=offset) for offset in range(lookback_days - 1, -1, -1)]
        catalyst_rows = [
            row
            for day in days
            for row in _read_jsonl(self.data_dir / "catalysts" / f"{day.isoformat()}.jsonl")
        ]
        idea_rows = [
            row
            for day in days
            for path in _idea_paths(self.data_dir, day)
            for row in _read_jsonl(path)
        ]
        warnings: list[str] = []
        catalyst_events = [event for row in catalyst_rows for event in row.get("events", [])]
        ideas = [idea for row in idea_rows for idea in row.get("ideas", [])]
        idea_follow = self._idea_follow_through(ideas, warnings)
        report = WorkflowEvaluationReport(
            as_of=as_of.isoformat(),
            lookback_days=lookback_days,
            catalyst_metrics=_catalyst_metrics(catalyst_events),
            idea_metrics={**_idea_metrics(ideas), **idea_follow},
            warnings=warnings,
        )
        return report

    def persist(self, report: WorkflowEvaluationReport) -> Path:
        path = self.data_dir / "evaluation" / "workflows" / f"{report.as_of}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(report.model_dump(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return path

    def _idea_follow_through(
        self,
        ideas: list[dict[str, Any]],
        warnings: list[str],
    ) -> dict[str, Any]:
        if self.provider is None:
            return {"follow_through_available": False}
        returns_1d: list[float] = []
        returns_3d: list[float] = []
        returns_5d: list[float] = []
        returns_10d: list[float] = []
        adverse_5d: list[float] = []
        evaluated = 0
        max_eval = 50
        for idea in ideas[-max_eval:]:
            symbol = str(idea.get("symbol") or "")
            if not symbol:
                continue
            try:
                bars = self.provider.get_bars(symbol, timeframe="1d", limit=180)
            except Exception as exc:
                warnings.append(f"{symbol}: bars unavailable for idea follow-through: {exc}")
                continue
            as_of = _parse_date_from_id_or_ref(idea)
            entry_idx = _first_bar_after(bars, as_of) if as_of else None
            if entry_idx is None or entry_idx >= len(bars):
                continue
            entry = bars[entry_idx].close
            future = bars[entry_idx + 1 : entry_idx + 11]
            r1 = _return_at(future, entry, 1)
            r3 = _return_at(future, entry, 3)
            r5 = _return_at(future, entry, 5)
            r10 = _return_at(future, entry, 10)
            mae = _max_adverse(future[:5], entry)
            if r1 is not None:
                returns_1d.append(r1)
            if r3 is not None:
                returns_3d.append(r3)
            if r5 is not None:
                returns_5d.append(r5)
            if r10 is not None:
                returns_10d.append(r10)
            if mae is not None:
                adverse_5d.append(mae)
            evaluated += 1
        return {
            "follow_through_available": True,
            "follow_through_limit": max_eval,
            "follow_through_evaluated": evaluated,
            "return_1d_average": _avg(returns_1d),
            "return_1d_hit_rate": _hit_rate(returns_1d),
            "return_3d_average": _avg(returns_3d),
            "return_3d_hit_rate": _hit_rate(returns_3d),
            "return_5d_average": _avg(returns_5d),
            "return_5d_hit_rate": _hit_rate(returns_5d),
            "return_10d_average": _avg(returns_10d),
            "return_10d_hit_rate": _hit_rate(returns_10d),
            "max_adverse_5d_average": _avg(adverse_5d),
            "sector_adjusted_return_available": False,
        }


def load_latest_workflow_evaluation(data_dir: Path) -> dict[str, Any] | None:
    root = data_dir / "evaluation" / "workflows"
    if not root.is_dir():
        return None
    files = sorted(root.glob("*.json"))
    if not files:
        return None
    try:
        return json.loads(files[-1].read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def _catalyst_metrics(events: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(events)
    by_type: dict[str, int] = {}
    for event in events:
        event_type = str(event.get("event_type") or "unknown")
        by_type[event_type] = by_type.get(event_type, 0) + 1
    with_sources = sum(1 for event in events if event.get("source_refs"))
    no_trade_ok = sum(1 for event in events if event.get("no_trade_disclaimer") is True)
    duplicate_count = total - len({str(event.get("event_id") or event.get("summary") or "") for event in events})
    stale_count = sum(1 for event in events if event.get("stale_status") in {"stale", "archived"})
    return {
        "event_count": total,
        "source_coverage": _ratio(with_sources, total),
        "no_trade_disclaimer_coverage": _ratio(no_trade_ok, total),
        "duplicate_rate": _ratio(duplicate_count, total),
        "stale_event_rate": _ratio(stale_count, total),
        "post_event_volatility_available": False,
        "false_positive_rate_available": False,
        "missed_event_rate_available": False,
        "by_type": by_type,
    }


def _idea_metrics(ideas: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(ideas)
    by_direction: dict[str, int] = {}
    for idea in ideas:
        direction = str(idea.get("direction") or "unknown")
        by_direction[direction] = by_direction.get(direction, 0) + 1
    with_sources = sum(1 for idea in ideas if idea.get("source_refs"))
    with_risks = sum(1 for idea in ideas if idea.get("risks"))
    with_next_step = sum(1 for idea in ideas if idea.get("next_step"))
    no_trade_ok = sum(1 for idea in ideas if idea.get("no_trade_disclaimer") is True)
    scores = [
        float(idea["score"])
        for idea in ideas
        if isinstance(idea.get("score"), int | float)
    ]
    return {
        "idea_count": total,
        "source_coverage": _ratio(with_sources, total),
        "risk_coverage": _ratio(with_risks, total),
        "next_step_coverage": _ratio(with_next_step, total),
        "no_trade_disclaimer_coverage": _ratio(no_trade_ok, total),
        "average_score": round(mean(scores), 4) if scores else None,
        "idea_conversion_rate_available": False,
        "risk_flag_accuracy_available": False,
        "score_component_usefulness_available": False,
        "repeated_false_positive_patterns": [],
        "by_direction": by_direction,
    }


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _idea_paths(data_dir: Path, day: date) -> list[Path]:
    root = data_dir / "ideas"
    return [
        root / f"{day.isoformat()}-morning.jsonl",
        root / f"{day.isoformat()}-weekly.jsonl",
        root / f"{day.isoformat()}-manual.jsonl",
        root / f"{day.isoformat()}.jsonl",
    ]


def _ratio(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return round(numerator / denominator, 4)


def _avg(values: list[float]) -> float | None:
    return round(mean(values), 4) if values else None


def _hit_rate(values: list[float]) -> float:
    if not values:
        return 0.0
    return round(sum(1 for value in values if value > 0) / len(values), 4)


def _first_bar_after(bars: list[Bar], target: date) -> int | None:
    for idx, bar in enumerate(bars):
        if bar.timestamp.date() > target:
            return idx
    return None


def _return_at(future: list[Bar], entry_close: float, days: int) -> float | None:
    if entry_close <= 0 or len(future) < days:
        return None
    return round((future[days - 1].close - entry_close) / entry_close, 4)


def _max_adverse(future: list[Bar], entry_close: float) -> float | None:
    if entry_close <= 0 or not future:
        return None
    return round((min(bar.low for bar in future) - entry_close) / entry_close, 4)


def _parse_date_from_id_or_ref(idea: dict[str, Any]) -> date | None:
    # The workflow artifact row owns `as_of`; idea rows are intentionally compact.
    # Until per-idea as_of is added, follow-through is best-effort and skipped.
    raw = idea.get("as_of")
    if not raw:
        return None
    try:
        return date.fromisoformat(str(raw))
    except ValueError:
        return None
