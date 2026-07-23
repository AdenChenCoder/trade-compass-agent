from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from trade_compass_agent.domain import AuditEvent, Recommendation, Signal


class AuditLog:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def record(self, event_type: str, summary: str, payload: dict | None = None) -> AuditEvent:
        event = AuditEvent(
            id=str(uuid4()),
            timestamp=datetime.now(),
            event_type=event_type,
            summary=summary,
            payload=payload or {},
        )
        self.events.append(event)
        return event

    def record_recommendation(
        self,
        signal: Signal,
        recommendation: Recommendation,
        *,
        provider_name: str,
        extra: dict | None = None,
    ) -> AuditEvent:
        payload = {
            "symbol": signal.symbol,
            "provider": provider_name,
            "grade_in": signal.grade.value,
            "grade_out": recommendation.action.value,
            "horizon": recommendation.horizon.value,
            "confidence": signal.confidence,
            "position_limit_pct": recommendation.position_limit_pct,
            "evidence": signal.evidence,
            "risks": signal.risks,
            "trigger": signal.trigger,
            "invalidation": recommendation.invalidation,
            "source_rules": signal.source_rules,
            "is_experimental": signal.is_experimental,
        }
        if extra:
            payload.update(extra)
        return self.record(
            event_type="recommendation",
            summary=f"{signal.symbol} {recommendation.action.value} ({recommendation.horizon.value})",
            payload=payload,
        )


class JsonAuditLog(AuditLog):
    def __init__(self, path: Path) -> None:
        super().__init__()
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.events = self._load()

    def record(self, event_type: str, summary: str, payload: dict | None = None) -> AuditEvent:
        event = super().record(event_type, summary, payload)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(self._serialize(event) + "\n")
        return event

    def recent(self, limit: int = 50) -> list[AuditEvent]:
        return list(reversed(self.events[-limit:]))

    def get(self, event_id: str) -> AuditEvent | None:
        for event in self.events:
            if event.id == event_id:
                return event
        return None

    def recommendations(self, limit: int = 50) -> list[AuditEvent]:
        events = [event for event in self.events if event.event_type == "recommendation"]
        return list(reversed(events[-limit:]))

    def trading_signals(self, limit: int = 50) -> list[AuditEvent]:
        events = [event for event in self.events if event.event_type == "trading_signal"]
        return list(reversed(events[-limit:]))

    def _load(self) -> list[AuditEvent]:
        if not self.path.exists():
            return []
        events: list[AuditEvent] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            raw = json.loads(line)
            events.append(
                AuditEvent(
                    id=str(raw["id"]),
                    timestamp=datetime.fromisoformat(raw["timestamp"]),
                    event_type=str(raw["event_type"]),
                    summary=str(raw["summary"]),
                    payload=dict(raw.get("payload", {})),
                )
            )
        return events

    @staticmethod
    def _serialize(event: AuditEvent) -> str:
        return json.dumps(
            {
                "id": event.id,
                "timestamp": event.timestamp.isoformat(),
                "event_type": event.event_type,
                "summary": event.summary,
                "payload": event.payload,
            },
            ensure_ascii=False,
        )
