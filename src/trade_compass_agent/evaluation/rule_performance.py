from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from statistics import mean

from trade_compass_agent.data import ChainProvider
from trade_compass_agent.data.providers import LocalBarCacheProvider
from trade_compass_agent.domain import AuditEvent
from trade_compass_agent.evaluation.follow_through import FollowThroughEvaluator
from trade_compass_agent.ops.audit import JsonAuditLog


@dataclass(frozen=True)
class RulePerformanceRow:
    rule_id: str
    title: str
    layer: str | None
    signal_count: int
    avg_return_1d: float | None
    avg_return_3d: float | None
    win_rate_1d: float | None
    experimental: bool = False


@dataclass(frozen=True)
class RulePerformanceReport:
    rows: list[RulePerformanceRow] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class RulePerformanceEvaluator:
    def __init__(
        self,
        *,
        data_dir: Path,
        memory_dir: Path,
        provider=None,
    ) -> None:
        self.audit = JsonAuditLog(data_dir / "audit.jsonl")
        self.provider = provider or ChainProvider(
            [LocalBarCacheProvider(data_dir / "market_cache")]
        )
        self.follow_through = FollowThroughEvaluator(self.provider)

    def evaluate(self, *, limit: int = 500) -> RulePerformanceReport:
        warnings: list[str] = []
        events = self.audit.recommendations(limit=limit)
        follow_report = self.follow_through.evaluate(self.audit.events, limit=limit)
        returns_by_audit = {item.audit_id: item for item in follow_report.results}
        warnings.extend(follow_report.warnings)

        counts: dict[str, int] = {}
        returns_1d: dict[str, list[float]] = {}
        returns_3d: dict[str, list[float]] = {}
        wins_1d: dict[str, list[bool]] = {}

        for event in events:
            source_rules = _source_rules(event)
            if not source_rules:
                continue
            ft = returns_by_audit.get(event.id)
            for rule_id in set(source_rules):
                counts[rule_id] = counts.get(rule_id, 0) + 1
                if ft is None:
                    continue
                if ft.return_1d is not None:
                    returns_1d.setdefault(rule_id, []).append(ft.return_1d)
                    wins_1d.setdefault(rule_id, []).append(ft.return_1d > 0)
                if ft.return_3d is not None:
                    returns_3d.setdefault(rule_id, []).append(ft.return_3d)

        all_rule_ids = sorted(counts)
        rows: list[RulePerformanceRow] = []
        for rule_id in all_rule_ids:
            r1 = returns_1d.get(rule_id, [])
            r3 = returns_3d.get(rule_id, [])
            w1 = wins_1d.get(rule_id, [])
            rows.append(
                RulePerformanceRow(
                    rule_id=rule_id,
                    title=rule_id,
                    layer=None,
                    signal_count=counts.get(rule_id, 0),
                    avg_return_1d=_avg(r1),
                    avg_return_3d=_avg(r3),
                    win_rate_1d=(sum(w1) / len(w1)) if w1 else None,
                    experimental=False,
                )
            )

        rows.sort(key=lambda item: (-item.signal_count, item.rule_id))
        return RulePerformanceReport(rows=rows, warnings=warnings)


def _source_rules(event: AuditEvent) -> list[str]:
    raw = event.payload.get("source_rules") or []
    if not isinstance(raw, list):
        return []
    return [str(item) for item in raw if item]


def _avg(values: list[float]) -> float | None:
    if not values:
        return None
    return round(mean(values), 4)
