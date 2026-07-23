from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from trade_compass_agent.config import PACKAGE_ROOT

BUILTIN_SPECIALIST_DIR = PACKAGE_ROOT / "specialists"


class SpecialistAssetError(ValueError):
    pass


@dataclass(frozen=True)
class ExecutionModel:
    type: str
    plan: str = ""
    max_rounds: int | None = None
    config: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SpecialistCapabilities:
    tools: tuple[str, ...] = ()
    skills: tuple[str, ...] = ()
    mcps: tuple[str, ...] = ()


@dataclass(frozen=True)
class SpecialistOutput:
    mode: str = "structured_markdown"
    schema: str = ""
    required_sections: tuple[str, ...] = ()


@dataclass(frozen=True)
class SpecialistProfile:
    id: str
    version: int
    name: str
    description: str
    kind: str
    execution_model: ExecutionModel
    capabilities: SpecialistCapabilities = field(default_factory=SpecialistCapabilities)
    prompts: dict[str, str] = field(default_factory=dict)
    output: SpecialistOutput = field(default_factory=SpecialistOutput)
    risk_policy: dict[str, Any] = field(default_factory=dict)
    path: Path | None = None


REQUIRED_SPECIALIST_FIELDS = (
    "id",
    "version",
    "name",
    "description",
    "kind",
    "execution_model",
    "capabilities",
    "output",
    "risk_policy",
)

SUPPORTED_EXECUTION_MODELS = {
    "single_agent_react",
    "graph_team",
    "debate_team",
    "managed_team",
    "population_simulation",
}


def load_specialist_profiles(directory: Path = BUILTIN_SPECIALIST_DIR) -> dict[str, SpecialistProfile]:
    profiles: dict[str, SpecialistProfile] = {}
    if not directory.is_dir():
        return profiles
    for path in sorted(directory.glob("*/specialist.yaml")):
        profile = load_specialist_profile(path)
        if profile.id in profiles:
            raise SpecialistAssetError(f"duplicate specialist id: {profile.id}")
        profiles[profile.id] = profile
    return profiles


def load_specialist_profile(path: Path) -> SpecialistProfile:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise SpecialistAssetError(f"{path}: specialist profile must be an object")
    missing = [field for field in REQUIRED_SPECIALIST_FIELDS if field not in raw]
    if missing:
        raise SpecialistAssetError(f"{path}: missing required fields: {', '.join(missing)}")
    specialist_id = str(raw["id"])
    if path.parent.name != specialist_id:
        raise SpecialistAssetError(f"{path}: specialist id must match folder name")
    execution_raw = raw.get("execution_model") or {}
    if not isinstance(execution_raw, dict):
        raise SpecialistAssetError(f"{path}: execution_model must be an object")
    execution_type = str(execution_raw.get("type") or "")
    if execution_type not in SUPPORTED_EXECUTION_MODELS:
        raise SpecialistAssetError(f"{path}: unsupported execution_model.type: {execution_type}")
    capabilities_raw = raw.get("capabilities") or {}
    if not isinstance(capabilities_raw, dict):
        raise SpecialistAssetError(f"{path}: capabilities must be an object")
    output_raw = raw.get("output") or {}
    if not isinstance(output_raw, dict):
        raise SpecialistAssetError(f"{path}: output must be an object")
    prompts = _resolve_prompts(path.parent, raw.get("prompts") or {})
    return SpecialistProfile(
        id=specialist_id,
        version=int(raw["version"]),
        name=str(raw["name"]),
        description=str(raw["description"]),
        kind=str(raw["kind"]),
        execution_model=ExecutionModel(
            type=execution_type,
            plan=str(execution_raw.get("plan") or ""),
            max_rounds=_optional_int(execution_raw.get("max_rounds")),
            config=dict(execution_raw.get("config") or {}),
        ),
        capabilities=SpecialistCapabilities(
            tools=_string_tuple(capabilities_raw.get("tools")),
            skills=_string_tuple(capabilities_raw.get("skills")),
            mcps=_string_tuple(capabilities_raw.get("mcps")),
        ),
        prompts=prompts,
        output=SpecialistOutput(
            mode=str(output_raw.get("mode") or "structured_markdown"),
            schema=str(output_raw.get("schema") or ""),
            required_sections=_string_tuple(output_raw.get("required_sections")),
        ),
        risk_policy=dict(raw.get("risk_policy") or {}),
        path=path,
    )


def _resolve_prompts(root: Path, raw: Any) -> dict[str, str]:
    if not isinstance(raw, dict):
        raise SpecialistAssetError(f"{root}: prompts must be an object")
    resolved: dict[str, str] = {}
    for name, prompt_path in raw.items():
        if not str(prompt_path):
            continue
        path = root / str(prompt_path)
        if not path.is_file():
            raise SpecialistAssetError(f"{root}: prompt file missing: {prompt_path}")
        resolved[str(name)] = path.read_text(encoding="utf-8")
    return resolved


def _string_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list | tuple):
        raise SpecialistAssetError(f"expected list, got {type(value).__name__}")
    return tuple(str(item) for item in value)


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    return int(value)
