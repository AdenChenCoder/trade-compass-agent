from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from trade_compass_agent.runtime.types import TurnEvent


@dataclass(frozen=True)
class AgentSpec:
    id: str
    role: str
    prompt: str = ""
    tools: tuple[str, ...] = ()
    skills: tuple[str, ...] = ()


@dataclass(frozen=True)
class TeamSpec:
    id: str
    strategy: str
    plan: str
    agents: tuple[AgentSpec, ...] = ()
    config: dict[str, Any] = field(default_factory=dict)


@dataclass
class RunState:
    team: TeamSpec
    task: str
    stack: Any
    config: Any = None
    client: Any = None
    on_event: Callable[[TurnEvent], None] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def record(self, event: str, data: dict[str, Any] | None = None) -> None:
        if self.on_event is None:
            return
        self.on_event(TurnEvent(event=event, data=data or {}))


@dataclass(frozen=True)
class MultiAgentRunResult:
    output: str
    metadata: dict[str, Any] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()


class CoordinationStrategy(Protocol):
    name: str

    def run(self, state: RunState) -> MultiAgentRunResult:
        ...
