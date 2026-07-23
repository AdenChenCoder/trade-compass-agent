from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class TurnEvent:
    event: str
    data: dict
    id: str | None = None


@dataclass(frozen=True)
class TurnSection:
    title: str
    content: str
    specialist: str | None = None
    symbols: list[str] = field(default_factory=list)
    kind: str | None = None
    forecast_data: dict | None = None


@dataclass(frozen=True)
class TurnResponse:
    session_id: str
    summary: str
    sections: list[TurnSection] = field(default_factory=list)
    turn_id: str | None = None
    interrupted: bool = False
