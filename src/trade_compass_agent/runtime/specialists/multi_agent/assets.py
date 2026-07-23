from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from trade_compass_agent.config import PROJECT_ROOT
from trade_compass_agent.runtime.specialists.assets import BUILTIN_SPECIALIST_DIR


class PlanAssetError(ValueError):
    pass


@dataclass(frozen=True)
class PlanAgentAsset:
    id: str
    role: str
    prompt: str
    tools: tuple[str, ...] = ()
    skills: tuple[str, ...] = ()


@dataclass(frozen=True)
class PlanStageAsset:
    id: str
    enabled: bool = True
    agents: tuple[str, ...] = ()


@dataclass(frozen=True)
class MultiAgentPlanAsset:
    id: str
    version: int
    strategy: str
    description: str
    defaults: dict[str, Any] = field(default_factory=dict)
    stages: dict[str, PlanStageAsset] = field(default_factory=dict)
    agents: dict[str, PlanAgentAsset] = field(default_factory=dict)
    path: Path | None = None

    def prompt(self, agent_id: str, fallback: str = "") -> str:
        agent = self.agents.get(agent_id)
        if agent is None or not agent.prompt.strip():
            return fallback
        return agent.prompt

    def stage_enabled(self, stage_id: str, fallback: bool = True) -> bool:
        stage = self.stages.get(stage_id)
        if stage is None:
            return fallback
        return stage.enabled

    def stage_agents(self, stage_id: str, fallback: tuple[str, ...] = ()) -> tuple[str, ...]:
        stage = self.stages.get(stage_id)
        if stage is None or not stage.agents:
            return fallback
        return stage.agents


def load_multi_agent_plan_asset(
    specialist_id: str,
    plan: str,
    *,
    specialist_dir: Path = BUILTIN_SPECIALIST_DIR,
) -> MultiAgentPlanAsset:
    path = specialist_dir / specialist_id / "plans" / f"{plan}.yaml"
    return load_plan_asset(path)


def load_plan_asset(path: Path) -> MultiAgentPlanAsset:
    if not path.is_file():
        raise PlanAssetError(f"{path}: plan manifest missing")
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise PlanAssetError(f"{path}: plan manifest must be an object")
    for field_name in ("id", "version", "strategy", "agents"):
        if field_name not in raw:
            raise PlanAssetError(f"{path}: missing required field: {field_name}")
    strategy_id = str(raw["id"])
    if path.stem != strategy_id:
        raise PlanAssetError(f"{path}: plan id must match file name")
    agents = _load_agents(path.parent / strategy_id, raw.get("agents") or {})
    stages = _load_stages(raw.get("stages") or {})
    for stage in stages.values():
        for agent_id in stage.agents:
            if agent_id not in agents:
                raise PlanAssetError(f"{path}: stage {stage.id} references unknown agent: {agent_id}")
    return MultiAgentPlanAsset(
        id=strategy_id,
        version=int(raw["version"]),
        strategy=str(raw["strategy"]),
        description=str(raw.get("description") or ""),
        defaults=dict(raw.get("defaults") or {}),
        stages=stages,
        agents=agents,
        path=path,
    )


def _load_agents(base: Path, raw: Any) -> dict[str, PlanAgentAsset]:
    if not isinstance(raw, dict):
        raise PlanAssetError("plan agents must be an object")
    agents: dict[str, PlanAgentAsset] = {}
    for agent_id, agent_raw in raw.items():
        if not isinstance(agent_raw, dict):
            raise PlanAssetError(f"plan agent {agent_id} must be an object")
        prompt_path = str(agent_raw.get("prompt") or "")
        if not prompt_path:
            raise PlanAssetError(f"plan agent {agent_id} prompt is required")
        path = base / prompt_path
        if not path.is_file():
            candidate = PROJECT_ROOT / prompt_path
            path = candidate if candidate.is_file() else path
        if not path.is_file():
            raise PlanAssetError(f"plan agent {agent_id} prompt missing: {prompt_path}")
        prompt = path.read_text(encoding="utf-8")
        agents[str(agent_id)] = PlanAgentAsset(
            id=str(agent_id),
            role=str(agent_raw.get("role") or agent_id),
            prompt=prompt,
            tools=_string_tuple(agent_raw.get("tools")),
            skills=_string_tuple(agent_raw.get("skills")),
        )
    return agents


def _load_stages(raw: Any) -> dict[str, PlanStageAsset]:
    if not isinstance(raw, dict):
        raise PlanAssetError("plan stages must be an object")
    stages: dict[str, PlanStageAsset] = {}
    for stage_id, stage_raw in raw.items():
        if not isinstance(stage_raw, dict):
            raise PlanAssetError(f"plan stage {stage_id} must be an object")
        stages[str(stage_id)] = PlanStageAsset(
            id=str(stage_id),
            enabled=bool(stage_raw.get("enabled", True)),
            agents=_string_tuple(stage_raw.get("agents")),
        )
    return stages


def _string_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list | tuple):
        raise PlanAssetError(f"expected list, got {type(value).__name__}")
    return tuple(str(item) for item in value)
