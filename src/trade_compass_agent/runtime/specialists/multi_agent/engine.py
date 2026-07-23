from __future__ import annotations

from trade_compass_agent.runtime.specialists.multi_agent.types import (
    CoordinationStrategy,
    MultiAgentRunResult,
    RunState,
)


class MultiAgentEngineError(ValueError):
    pass


class MultiAgentEngine:
    def __init__(self) -> None:
        self._strategies: dict[str, CoordinationStrategy] = {}

    def register_strategy(self, strategy: CoordinationStrategy) -> None:
        self._strategies[strategy.name] = strategy

    def run(self, state: RunState) -> MultiAgentRunResult:
        strategy = self._strategies.get(state.team.strategy)
        if strategy is None:
            raise MultiAgentEngineError(
                f"unsupported multi-agent strategy: {state.team.strategy}/{state.team.plan}"
            )
        state.record(
            "multi_agent.started",
            {
                "team_id": state.team.id,
                "strategy": state.team.strategy,
                "plan": state.team.plan,
            },
        )
        try:
            result = strategy.run(state)
        except Exception as exc:
            state.record(
                "multi_agent.failed",
                {
                    "team_id": state.team.id,
                    "strategy": state.team.strategy,
                    "plan": state.team.plan,
                    "error": f"{type(exc).__name__}: {exc}",
                },
            )
            raise
        state.record(
            "multi_agent.finished",
            {
                "team_id": state.team.id,
                "strategy": state.team.strategy,
                "plan": state.team.plan,
                "bytes": len(result.output.encode("utf-8")),
                "warnings": list(result.warnings),
            },
        )
        return result


def default_multi_agent_engine() -> MultiAgentEngine:
    from trade_compass_agent.runtime.specialists.multi_agent.strategies.debate_team import (
        DebateTeamStrategy,
    )

    engine = MultiAgentEngine()
    engine.register_strategy(DebateTeamStrategy())
    return engine
