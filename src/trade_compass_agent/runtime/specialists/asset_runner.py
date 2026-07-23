"""Generic specialist runner for folder-backed specialist assets."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from trade_compass_agent.config import AppConfig
from trade_compass_agent.llm.providers import ChatClient, ChatMessage, create_chat_client
from trade_compass_agent.runtime.exceptions import AgentUnavailableError
from trade_compass_agent.runtime.market_stack import MarketStack
from trade_compass_agent.runtime.schema_validator import validate_schema
from trade_compass_agent.runtime.specialists.assets import SpecialistProfile
from trade_compass_agent.runtime.specialists.multi_agent.engine import MultiAgentEngineError, default_multi_agent_engine
from trade_compass_agent.runtime.specialists.multi_agent.types import RunState, TeamSpec
from trade_compass_agent.runtime.specialists.nested_loop import run_react_loop
from trade_compass_agent.runtime.tools.policy import default_tool_policy
from trade_compass_agent.runtime.tools.readers import READER_TOOL_SCHEMAS, run_reader_tool
from trade_compass_agent.runtime.types import TurnEvent


def run_asset_specialist(
    stack: MarketStack,
    profile: SpecialistProfile,
    task: str,
    *,
    config: AppConfig | None = None,
    on_event: Callable[[TurnEvent], None] | None = None,
    client: ChatClient | None = None,
) -> str:
    """Run a specialist directly from its asset profile."""
    app_config = config or stack.config
    execution_type = profile.execution_model.type
    if execution_type == "single_agent_react":
        return _run_single_agent_react(
            stack,
            profile,
            task,
            config=app_config,
            on_event=on_event,
            client=client,
        )
    if execution_type in {"debate_team", "graph_team", "managed_team", "population_simulation"}:
        return _run_multi_agent_profile(
            stack,
            profile,
            task,
            config=app_config,
            on_event=on_event,
        )
    return json.dumps(
        {
            "error": f"unsupported specialist execution model: {execution_type}",
            "specialist": profile.id,
        },
        ensure_ascii=False,
    )


def structure_specialist_output(profile: SpecialistProfile, output: str) -> dict[str, Any]:
    """Wrap a specialist report in its declared production output contract."""
    parsed = _parse_json_object(output)
    if profile.output.mode == "structured_markdown":
        payload = {
            "report_markdown": _report_markdown(output, parsed),
            "source_refs": _source_refs(parsed),
            "warnings": _warnings(parsed),
            "metadata": {
                "specialist_id": profile.id,
                "execution_model": profile.execution_model.type,
                "plan": profile.execution_model.plan,
                "profile_version": profile.version,
            },
        }
    else:
        payload = parsed if parsed is not None else {"raw_output": output}
        metadata = payload.setdefault("metadata", {})
        if isinstance(metadata, dict):
            metadata.setdefault("specialist_id", profile.id)
            metadata.setdefault("execution_model", profile.execution_model.type)
            metadata.setdefault("plan", profile.execution_model.plan)
            metadata.setdefault("profile_version", profile.version)
        payload.setdefault("warnings", [])
    if profile.output.schema and profile.path is not None:
        schema_path = profile.path.parent / profile.output.schema
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        validate_schema(payload, schema)
    return payload


def _run_single_agent_react(
    stack: MarketStack,
    profile: SpecialistProfile,
    task: str,
    *,
    config: AppConfig,
    on_event: Callable[[TurnEvent], None] | None,
    client: ChatClient | None,
) -> str:
    try:
        chat_client = client or create_chat_client(config)
    except AgentUnavailableError as exc:
        return json.dumps(
            {"error": f"{profile.id} unavailable: {exc}", "specialist": profile.id},
            ensure_ascii=False,
        )

    from trade_compass_agent.runtime.tools.registry import ToolRegistry

    registry = ToolRegistry(stack, on_event=on_event)
    allowed_tools = set(profile.capabilities.tools)
    tool_schemas = _allowed_tool_schemas(registry.schemas, allowed_tools)
    messages = [
        ChatMessage(role="system", content=_system_prompt(profile, config)),
        ChatMessage(role="user", content=task),
    ]

    def on_tool_event(event: str, data: dict[str, Any]) -> None:
        if on_event:
            on_event(TurnEvent(event=event, data={**data, "specialist": profile.id}))

    return run_react_loop(
        client=chat_client,
        messages=messages,
        tool_schemas=tool_schemas,
        execute_tool=lambda name, args: _execute_allowed_tool(registry, profile, name, args),
        max_rounds=profile.execution_model.max_rounds or 6,
        on_tool_event=on_tool_event,
    )


def _run_multi_agent_profile(
    stack: MarketStack,
    profile: SpecialistProfile,
    task: str,
    *,
    config: AppConfig,
    on_event: Callable[[TurnEvent], None] | None,
) -> str:
    try:
        result = default_multi_agent_engine().run(
            RunState(
                team=TeamSpec(
                    id=profile.id,
                    strategy=profile.execution_model.type,
                    plan=profile.execution_model.plan,
                    agents=tuple(profile.execution_model.config.get("agents") or ()),
                    config=profile.execution_model.config,
                ),
                task=task,
                stack=stack,
                config=config,
                on_event=on_event,
            )
        )
    except MultiAgentEngineError as exc:
        return json.dumps(
            {
                "error": str(exc),
                "specialist": profile.id,
                "execution_model": profile.execution_model.type,
                "plan": profile.execution_model.plan,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    return result.output


def _system_prompt(profile: SpecialistProfile, config: AppConfig) -> str:
    prompt = profile.prompts.get("system", "").strip()
    skill_text = _skill_text(profile, config)
    required_sections = ", ".join(profile.output.required_sections)
    output_instruction = ""
    if required_sections:
        output_instruction = f"\nRequired output sections/fields: {required_sections}."
    risk_instruction = ""
    if profile.risk_policy.get("may_recommend_trade") is False:
        risk_instruction = (
            "\nRisk policy: do not produce broker orders or claim this is executable trading advice."
        )
    return (
        f"You are the {profile.id} specialist.\n"
        f"{profile.description}\n"
        f"{prompt}"
        f"{skill_text}"
        f"{output_instruction}"
        f"{risk_instruction}"
    ).strip()


def _parse_json_object(output: str) -> dict[str, Any] | None:
    try:
        parsed = json.loads(output)
    except (json.JSONDecodeError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _report_markdown(output: str, parsed: dict[str, Any] | None) -> str:
    if parsed is None:
        return output
    report = parsed.get("report_markdown")
    if isinstance(report, str) and report.strip():
        return report
    if "error" in parsed:
        return json.dumps(parsed, ensure_ascii=False, sort_keys=True)
    return output


def _source_refs(parsed: dict[str, Any] | None) -> list[str]:
    if parsed is None:
        return []
    raw = parsed.get("source_refs")
    if isinstance(raw, list):
        return [str(item) for item in raw if str(item).strip()]
    if isinstance(raw, str) and raw.strip():
        return [raw]
    return []


def _warnings(parsed: dict[str, Any] | None) -> list[str]:
    if parsed is None:
        return []
    raw = parsed.get("warnings")
    if isinstance(raw, list):
        return [str(item) for item in raw if str(item).strip()]
    if "error" in parsed:
        return [str(parsed["error"])]
    return []


def _skill_text(profile: SpecialistProfile, config: AppConfig) -> str:
    if not profile.capabilities.skills:
        return ""
    from trade_compass_agent.runtime.skills import discover_skills, load_skill_body

    discovered = {
        skill.name: skill
        for skill in discover_skills(memory_dir=config.memory_dir)
    }
    blocks: list[str] = []
    for name in profile.capabilities.skills:
        skill = discovered.get(name)
        if skill is None:
            blocks.append(f"\n\n## Skill: {name}\n(skill {name} not found)")
        else:
            blocks.append(f"\n\n## Skill: {name}\n{load_skill_body(skill)}")
    return "".join(blocks)


def _allowed_tool_schemas(
    registry_schemas: list[dict[str, Any]],
    allowed_tools: set[str],
) -> list[dict[str, Any]]:
    schemas = [*registry_schemas, *READER_TOOL_SCHEMAS]
    return [
        schema
        for schema in schemas
        if str(schema.get("function", {}).get("name") or "") in allowed_tools
    ]


def _execute_allowed_tool(
    registry: Any,
    profile: SpecialistProfile,
    name: str,
    args: dict[str, Any],
) -> str:
    policy = default_tool_policy()
    try:
        descriptor = policy.require_allowed(name, set(profile.capabilities.tools))
    except Exception as exc:
        return json.dumps(
            {"error": str(exc), "tool": name, "specialist": profile.id},
            ensure_ascii=False,
        )
    if descriptor.category == "reader":
        try:
            return run_reader_tool(name, **args)
        except Exception as exc:
            return json.dumps(
                {"error": f"{type(exc).__name__}: {exc}", "tool": name},
                ensure_ascii=False,
            )
    return registry.execute(name, args)
