#!/usr/bin/env python3
from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

ERRORS: list[str] = []

KNOWN_READERS = {
    "announcement_reader",
    "news_reader",
    "research_report_reader",
    "kol_signal_reader",
    "webpage_reader",
}

REQUIRED_WORKFLOW_FIELDS = (
    "id",
    "version",
    "name",
    "description",
    "owner",
    "runner",
    "inputs",
    "tools",
    "skills",
    "readers",
    "output_schema",
    "persistence",
    "risk_policy",
    "timeout_seconds",
    "retry_policy",
    "degradation_policy",
    "evaluation_hooks",
)

REQUIRED_WORKFLOW_IDS = {
    "equity_research",
    "intraday_tech",
    "risk_advisor",
    "close_check",
    "postmarket_archive",
    "premarket_briefing",
    "morning_plan",
    "catalyst_calendar_cn",
    "idea_generation_cn",
    "eod_review",
    "weekend_review",
}

REQUIRED_WORKFLOW_ASSET_IDS = REQUIRED_WORKFLOW_IDS

SUPPORTED_SPECIALIST_EXECUTION_MODELS = {
    "single_agent_react",
    "graph_team",
    "debate_team",
    "managed_team",
    "population_simulation",
}

PROMPT_RUNTIME_LEAK_PATTERNS = (
    "MultiAgentEngine",
    "debate_v2",
    "adapter",
    "runtime",
    "registry",
    "plan asset",
    "src/",
    "trade_compass_agent",
    ".py",
    ".yaml",
)


def main() -> int:
    _check_schemas()
    skills = _check_skills()
    tools = _tool_names()
    _check_specialists(tools=tools, skills=skills)
    workflow_ids = _check_workflows(skills=skills, tools=tools)
    specialist_ids = _specialist_asset_ids()
    _check_workflow_assets(tools=tools, specialist_ids=specialist_ids, legacy_workflow_ids=workflow_ids)
    _check_jobs()
    if ERRORS:
        print(f"FAIL - {len(ERRORS)} asset issue(s):", file=sys.stderr)
        for err in ERRORS:
            print(f"  - {err}", file=sys.stderr)
        return 1
    print("OK - asset checks passed.")
    return 0


def _err(message: str) -> None:
    ERRORS.append(message)


def _check_schemas() -> None:
    try:
        from jsonschema.exceptions import SchemaError
        from jsonschema.validators import validator_for
    except ModuleNotFoundError:
        _err("jsonschema is required for schema metaschema validation; install .[dev]")
        return

    for path in sorted((ROOT / "schemas").rglob("*.json")):
        try:
            schema = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            _err(f"schema JSON parse failed: {_rel(path)}: {exc}")
            continue
        if not isinstance(schema, dict):
            _err(f"schema must be an object: {_rel(path)}")
            continue
        if schema.get("$schema") and "json-schema.org" not in str(schema["$schema"]):
            _err(f"schema has unknown metaschema: {_rel(path)}")
        if "type" not in schema and "properties" not in schema and "$id" not in schema:
            _err(f"schema missing type/properties/$id: {_rel(path)}")
        try:
            validator_for(schema).check_schema(schema)
        except SchemaError as exc:
            _err(f"schema metaschema validation failed: {_rel(path)}: {exc.message}")


def _check_skills() -> set[str]:
    roots = [ROOT / ".trade-compass" / "skills"]
    names: set[str] = set()
    seen_paths: dict[str, Path] = {}
    for root in roots:
        if not root.is_dir():
            continue
        for skill_md in sorted(root.glob("*/SKILL.md")):
            name, description = _parse_skill(skill_md)
            if not name:
                _err(f"skill missing name: {_rel(skill_md)}")
                name = skill_md.parent.name
            if not description:
                _err(f"skill missing description: {_rel(skill_md)}")
            if name in seen_paths:
                _err(f"duplicate skill name {name!r}: {_rel(seen_paths[name])} and {_rel(skill_md)}")
            seen_paths[name] = skill_md
            names.add(name)
            ref_dir = skill_md.parent / "references"
            if ref_dir.exists() and not ref_dir.is_dir():
                _err(f"skill references path is not a directory: {_rel(ref_dir)}")

    cfg = ROOT / "config" / "agent_skills.yaml"
    if cfg.is_file():
        try:
            raw = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            _err(f"agent_skills.yaml parse failed: {exc}")
            raw = {}
        for name in raw.get("enabled_skills") or []:
            if str(name) not in names:
                _err(f"agent_skills.yaml enables unknown skill: {name}")
    return names


def _parse_skill(path: Path) -> tuple[str, str]:
    text = path.read_text(encoding="utf-8")
    fallback_name = path.parent.name
    if not text.startswith("---"):
        first = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
        return fallback_name, first.lstrip("#").strip()
    end = text.find("---", 3)
    if end == -1:
        return fallback_name, ""
    try:
        meta = yaml.safe_load(text[3:end]) or {}
    except yaml.YAMLError as exc:
        _err(f"skill frontmatter parse failed: {_rel(path)}: {exc}")
        return fallback_name, ""
    return str(meta.get("name") or fallback_name), str(meta.get("description") or "")


def _check_workflows(*, skills: set[str], tools: set[str]) -> set[str]:
    workflow_dir = ROOT / "config" / "workflows"
    if not workflow_dir.is_dir():
        return set()
    for path in sorted(workflow_dir.glob("*.yaml")):
        _err(
            "legacy runner-based builtin workflow manifest is not allowed; "
            f"move it to src/trade_compass_agent/workflows/<id>/workflow.yaml: {_rel(path)}"
        )
    return set()


def _check_workflow_assets(
    *,
    tools: set[str],
    specialist_ids: set[str],
    legacy_workflow_ids: set[str],
) -> None:
    root = ROOT / "src" / "trade_compass_agent" / "workflows"
    if not root.is_dir():
        return
    ids: set[str] = set()
    workflow_refs: dict[str, list[str]] = {}
    manifests: list[tuple[Path, dict[str, Any], str]] = []
    for path in sorted(root.glob("*/workflow.yaml")):
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            _err(f"workflow asset YAML parse failed: {_rel(path)}: {exc}")
            continue
        if not isinstance(raw, dict):
            _err(f"workflow asset {_rel(path)} must be an object")
            continue
        workflow_id = str(raw.get("id") or path.parent.name)
        if workflow_id in ids:
            _err(f"duplicate workflow asset id: {workflow_id}")
        ids.add(workflow_id)
        manifests.append((path, raw, workflow_id))
    known_workflow_ids = ids | legacy_workflow_ids
    for path, raw, workflow_id in manifests:
        if path.parent.name != workflow_id:
            _err(f"workflow asset id must match folder name: {_rel(path)}")
        for field in (
            "id",
            "version",
            "name",
            "description",
            "owner",
            "inputs",
            "steps",
            "output_schema",
            "persistence",
            "risk_policy",
            "timeout_seconds",
            "retry_policy",
            "degradation_policy",
            "evaluation_hooks",
        ):
            if field not in raw:
                _err(f"workflow asset {_rel(path)} missing required field: {field}")
        inputs = raw.get("inputs") or {}
        if not isinstance(inputs, dict):
            _err(f"workflow asset {workflow_id} inputs must be object")
        schema = str(raw.get("output_schema") or "")
        if schema:
            schema_path = _asset_relative_or_root_path(path.parent, schema)
            if not schema_path.is_file():
                _err(f"workflow asset {workflow_id} output_schema missing: {schema}")
            else:
                _check_schema_file(schema_path)
        risk_policy = raw.get("risk_policy") or {}
        if not isinstance(risk_policy, dict):
            _err(f"workflow asset {workflow_id} risk_policy must be object")
        elif risk_policy.get("may_recommend_trade") is not False:
            _err(f"workflow asset {workflow_id} risk_policy.may_recommend_trade must be false")
        persistence = raw.get("persistence") or {}
        if not isinstance(persistence, dict):
            _err(f"workflow asset {workflow_id} persistence must be object")
        else:
            template = str(persistence.get("path_template") or "")
            if persistence.get("kind") != "jsonl":
                _err(f"workflow asset {workflow_id} persistence.kind must be jsonl")
            if not template:
                _err(f"workflow asset {workflow_id} persistence.path_template missing")
            elif not template.startswith("data/"):
                _err(f"workflow asset {workflow_id} persistence must write under data/: {template}")
            if "retention_days" not in persistence:
                _err(f"workflow asset {workflow_id} persistence.retention_days missing")
        retry = raw.get("retry_policy") or {}
        if not isinstance(retry, dict):
            _err(f"workflow asset {workflow_id} retry_policy must be object")
        elif "max_retries" not in retry or "backoff_seconds" not in retry:
            _err(f"workflow asset {workflow_id} retry_policy must declare max_retries and backoff_seconds")
        degradation = raw.get("degradation_policy") or {}
        if not isinstance(degradation, dict):
            _err(f"workflow asset {workflow_id} degradation_policy must be object")
        hooks = raw.get("evaluation_hooks") or []
        if not isinstance(hooks, list):
            _err(f"workflow asset {workflow_id} evaluation_hooks must be list")
        steps = raw.get("steps") or []
        if not isinstance(steps, list):
            _err(f"workflow asset {workflow_id} steps must be list")
            continue
        workflow_refs[workflow_id] = [
            str(step.get("uses") or "").removeprefix("workflow:")
            for step in steps
            if isinstance(step, dict)
            and str(step.get("type") or "") == "workflow"
            and str(step.get("uses") or "").startswith("workflow:")
        ]
        _check_workflow_asset_steps(
            workflow_id,
            steps,
            tools=tools,
            specialist_ids=specialist_ids,
            workflow_ids=known_workflow_ids,
        )
    _check_workflow_reference_cycles(workflow_refs)
    missing = sorted(REQUIRED_WORKFLOW_ASSET_IDS - ids)
    if missing:
        _err(f"missing required workflow asset ids: {', '.join(missing)}")


def _check_workflow_asset_steps(
    workflow_id: str,
    steps: list[Any],
    *,
    tools: set[str],
    specialist_ids: set[str],
    workflow_ids: set[str],
) -> None:
    step_ids: set[str] = set()
    dependencies: dict[str, list[str]] = {}
    primary_outputs = 0
    for raw_step in steps:
        if not isinstance(raw_step, dict):
            _err(f"workflow asset {workflow_id} step must be object")
            continue
        step_id = str(raw_step.get("id") or "")
        step_type = str(raw_step.get("type") or "")
        uses = str(raw_step.get("uses") or "")
        if not step_id:
            _err(f"workflow asset {workflow_id} step missing id")
            continue
        if step_id in step_ids:
            _err(f"workflow asset {workflow_id} duplicate step id: {step_id}")
        step_ids.add(step_id)
        if step_type not in {"tool", "specialist", "workflow", "compose", "evaluate"}:
            _err(f"workflow asset {workflow_id} step {step_id} unsupported type: {step_type}")
        if bool(raw_step.get("primary_output")):
            primary_outputs += 1
        if "when" in raw_step and not isinstance(raw_step.get("when"), dict):
            _err(f"workflow asset {workflow_id} step {step_id} when must be object")
        if step_type == "tool" and not _known_prefixed_ref(uses, "tool", tools):
            _err(f"workflow asset {workflow_id} step {step_id} references unknown tool: {uses}")
        if step_type == "specialist" and not _known_prefixed_ref(uses, "specialist", specialist_ids):
            _err(f"workflow asset {workflow_id} step {step_id} references unknown specialist: {uses}")
        if step_type == "workflow" and not _known_prefixed_ref(uses, "workflow", workflow_ids):
            _err(f"workflow asset {workflow_id} step {step_id} references unknown workflow: {uses}")
        depends_on = raw_step.get("depends_on") or []
        if not isinstance(depends_on, list):
            _err(f"workflow asset {workflow_id} step {step_id} depends_on must be list")
            depends_on = []
        dependencies[step_id] = [str(dep) for dep in depends_on]
    for step_id, deps in dependencies.items():
        for dep in deps:
            if dep not in step_ids:
                _err(f"workflow asset {workflow_id} step {step_id} depends on unknown step: {dep}")
    if primary_outputs > 1:
        _err(f"workflow asset {workflow_id} must not declare multiple primary_output steps")
    _check_step_cycles(workflow_id, dependencies)


def _known_prefixed_ref(value: str, prefix: str, known: set[str]) -> bool:
    expected = f"{prefix}:"
    return value.startswith(expected) and value.removeprefix(expected) in known


def _check_step_cycles(workflow_id: str, dependencies: dict[str, list[str]]) -> None:
    temporary: set[str] = set()
    permanent: set[str] = set()

    def visit(step_id: str) -> None:
        if step_id in permanent:
            return
        if step_id in temporary:
            _err(f"workflow asset {workflow_id} has step cycle at {step_id}")
            return
        temporary.add(step_id)
        for dep in dependencies.get(step_id, []):
            visit(dep)
        temporary.remove(step_id)
        permanent.add(step_id)

    for step_id in dependencies:
        visit(step_id)


def _check_workflow_reference_cycles(graph: dict[str, list[str]]) -> None:
    visiting: list[str] = []
    visited: set[str] = set()

    def visit(workflow_id: str) -> None:
        if workflow_id in visited:
            return
        if workflow_id in visiting:
            cycle = visiting[visiting.index(workflow_id):] + [workflow_id]
            _err(f"workflow asset reference cycle: {' -> '.join(cycle)}")
            return
        visiting.append(workflow_id)
        for child_id in graph.get(workflow_id, []):
            if child_id in graph:
                visit(child_id)
        visiting.pop()
        visited.add(workflow_id)

    for workflow_id in sorted(graph):
        visit(workflow_id)


def _check_jobs() -> None:
    try:
        from trade_compass_agent.config import SchedulerConfig
        from trade_compass_agent.ops.job_definition import _builtin_jobs
    except Exception as exc:
        _err(f"cannot import job definitions: {type(exc).__name__}: {exc}")
        return
    workflow_ids = _workflow_ids()
    for job in _builtin_jobs(SchedulerConfig()):
        if not job.workflow_id:
            _err(f"built-in job {job.id} must bind a workflow id")
        elif job.workflow_id not in workflow_ids:
            _err(f"built-in job {job.id} references unknown workflow id: {job.workflow_id}")


def _workflow_ids() -> set[str]:
    ids: set[str] = set()
    for path in (ROOT / "src" / "trade_compass_agent" / "workflows").glob("*/workflow.yaml"):
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:
            continue
        workflow_id = str(raw.get("id") or "")
        if workflow_id:
            ids.add(workflow_id)
    return ids


def _check_specialists(*, tools: set[str], skills: set[str]) -> None:
    _check_specialist_assets(tools=tools, skills=skills)
    asset_ids = _specialist_asset_ids()
    _check_specialist_runtime_modules(asset_ids=asset_ids)


def _check_specialist_runtime_modules(*, asset_ids: set[str]) -> None:
    root = ROOT / "src" / "trade_compass_agent" / "runtime" / "specialists"
    if not root.is_dir():
        return
    allowed_runtime_modules = {
        "__init__.py",
        "asset_runner.py",
        "assets.py",
        "nested_loop.py",
        "registry.py",
        "risk_controls.py",
        "run.py",
        "signal_parsing.py",
        "situation.py",
    }
    names: set[str] = set()
    for path in sorted(root.glob("*.py")):
        if path.name in allowed_runtime_modules:
            continue
        name = path.stem
        if name in asset_ids:
            _err(
                "specialist runtime must not define concrete specialist modules: "
                f"{_rel(path)}"
            )
        else:
            _err(f"unexpected specialist runtime module: {_rel(path)}")
        if name in names:
            _err(f"duplicate specialist module: {name}")
        names.add(name)
        text = path.read_text(encoding="utf-8")
        if not text.lstrip().startswith('"""'):
            _err(f"specialist missing public description docstring: {_rel(path)}")


def _check_specialist_assets(*, tools: set[str], skills: set[str]) -> None:
    root = ROOT / "src" / "trade_compass_agent" / "specialists"
    if not root.is_dir():
        return
    ids: set[str] = set()
    for path in sorted(root.glob("*/specialist.yaml")):
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            _err(f"specialist YAML parse failed: {_rel(path)}: {exc}")
            continue
        if not isinstance(raw, dict):
            _err(f"specialist {_rel(path)} must be an object")
            continue
        specialist_id = str(raw.get("id") or path.parent.name)
        if specialist_id in ids:
            _err(f"duplicate specialist id: {specialist_id}")
        ids.add(specialist_id)
        if path.parent.name != specialist_id:
            _err(f"specialist id must match folder name: {_rel(path)}")
        for field in (
            "id",
            "version",
            "name",
            "description",
            "kind",
            "execution_model",
            "capabilities",
            "output",
            "risk_policy",
        ):
            if field not in raw:
                _err(f"specialist {_rel(path)} missing required field: {field}")
        if raw.get("kind") != "specialist":
            _err(f"specialist {specialist_id} kind must be specialist")
        for forbidden in ("tests", "fixtures", "evals"):
            if (path.parent / forbidden).exists():
                _err(f"specialist {specialist_id} folder must not contain {forbidden}/")
        execution = _dict_field(raw, "execution_model", specialist_id)
        execution_type = str(execution.get("type") or "")
        if execution_type not in SUPPORTED_SPECIALIST_EXECUTION_MODELS:
            _err(f"specialist {specialist_id} unsupported execution_model.type: {execution_type}")
        if execution_type in {"graph_team", "debate_team", "managed_team", "population_simulation"} and not execution.get("plan"):
            _err(f"specialist {specialist_id} {execution_type} requires execution_model.plan")
        capabilities = _dict_field(raw, "capabilities", specialist_id)
        for tool in capabilities.get("tools") or []:
            if str(tool) not in tools:
                _err(f"specialist {specialist_id} references unknown tool: {tool}")
        for skill in capabilities.get("skills") or []:
            if str(skill) not in skills:
                _err(f"specialist {specialist_id} references unknown skill: {skill}")
        prompts = raw.get("prompts") or {}
        if prompts and not isinstance(prompts, dict):
            _err(f"specialist {specialist_id} prompts must be object")
            prompts = {}
        for prompt in prompts.values():
            prompt_path = path.parent / str(prompt)
            if not prompt_path.is_file():
                _err(f"specialist {specialist_id} prompt file missing: {prompt}")
            else:
                _check_prompt_has_no_runtime_leak(prompt_path, owner=specialist_id)
        output = _dict_field(raw, "output", specialist_id)
        schema = str(output.get("schema") or "")
        if schema:
            schema_path = path.parent / schema
            if not schema_path.is_file():
                _err(f"specialist {specialist_id} output schema missing: {schema}")
            else:
                _check_schema_file(schema_path)
        risk_policy = _dict_field(raw, "risk_policy", specialist_id)
        if risk_policy.get("may_recommend_trade") is not False:
            _err(f"specialist {specialist_id} risk_policy.may_recommend_trade must be false")
        _check_plan_assets(path.parent, specialist_id, tools=tools, skills=skills)


def _check_plan_assets(
    specialist_root: Path,
    specialist_id: str,
    *,
    tools: set[str],
    skills: set[str],
) -> None:
    plans_root = specialist_root / "plans"
    if not plans_root.is_dir():
        return
    ids: set[str] = set()
    for path in sorted(plans_root.glob("*.yaml")):
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            _err(f"plan YAML parse failed: {_rel(path)}: {exc}")
            continue
        if not isinstance(raw, dict):
            _err(f"plan {_rel(path)} must be object")
            continue
        plan_id = str(raw.get("id") or path.stem)
        if plan_id in ids:
            _err(f"duplicate plan id for {specialist_id}: {plan_id}")
        ids.add(plan_id)
        if path.stem != plan_id:
            _err(f"plan id must match file name: {_rel(path)}")
        for field in ("id", "version", "strategy", "agents"):
            if field not in raw:
                _err(f"plan {_rel(path)} missing required field: {field}")
        defaults = raw.get("defaults") or {}
        if defaults and not isinstance(defaults, dict):
            _err(f"plan {plan_id} defaults must be object")
        stages = raw.get("stages") or {}
        agents = raw.get("agents") or {}
        if stages and not isinstance(stages, dict):
            _err(f"plan {plan_id} stages must be object")
            stages = {}
        if not isinstance(agents, dict):
            _err(f"plan {plan_id} agents must be object")
            agents = {}
        known_agents = {str(agent_id) for agent_id in agents}
        for stage_id, stage in stages.items():
            if not isinstance(stage, dict):
                _err(f"plan {plan_id} stage {stage_id} must be object")
                continue
            for agent_id in stage.get("agents") or []:
                if str(agent_id) not in known_agents:
                    _err(f"plan {plan_id} stage {stage_id} references unknown agent: {agent_id}")
        for agent_id, agent in agents.items():
            if not isinstance(agent, dict):
                _err(f"plan {plan_id} agent {agent_id} must be object")
                continue
            prompt = str(agent.get("prompt") or "")
            if not prompt:
                _err(f"plan {plan_id} agent {agent_id} prompt is required")
            else:
                prompt_path = plans_root / plan_id / prompt
                if not prompt_path.is_file():
                    _err(f"plan {plan_id} agent {agent_id} prompt missing: {prompt}")
                else:
                    _check_prompt_has_no_runtime_leak(
                        prompt_path,
                        owner=f"{specialist_id}/{plan_id}/{agent_id}",
                    )
            for tool in agent.get("tools") or []:
                if str(tool) not in tools:
                    _err(f"plan {plan_id} agent {agent_id} references unknown tool: {tool}")
            for skill in agent.get("skills") or []:
                if str(skill) not in skills:
                    _err(f"plan {plan_id} agent {agent_id} references unknown skill: {skill}")


def _specialist_asset_ids() -> set[str]:
    root = ROOT / "src" / "trade_compass_agent" / "specialists"
    ids: set[str] = set()
    if not root.is_dir():
        return ids
    for path in sorted(root.glob("*/specialist.yaml")):
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:
            continue
        if isinstance(raw, dict):
            specialist_id = str(raw.get("id") or "")
            if specialist_id:
                ids.add(specialist_id)
    return ids


def _dict_field(raw: dict[str, Any], field: str, owner: str) -> dict[str, Any]:
    value = raw.get(field) or {}
    if not isinstance(value, dict):
        _err(f"{owner} {field} must be object")
        return {}
    return value


def _check_schema_file(path: Path) -> None:
    try:
        from jsonschema.exceptions import SchemaError
        from jsonschema.validators import validator_for
    except ModuleNotFoundError:
        return
    try:
        schema = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        _err(f"schema JSON parse failed: {_rel(path)}: {exc}")
        return
    try:
        validator_for(schema).check_schema(schema)
    except SchemaError as exc:
        _err(f"schema metaschema validation failed: {_rel(path)}: {exc.message}")


def _check_prompt_has_no_runtime_leak(path: Path, *, owner: str) -> None:
    text = path.read_text(encoding="utf-8")
    for pattern in PROMPT_RUNTIME_LEAK_PATTERNS:
        if pattern in text:
            _err(
                f"prompt for {owner} leaks runtime detail {pattern!r}: {_rel(path)}"
            )


def _asset_relative_or_root_path(base: Path, raw_path: str) -> Path:
    candidate = base / raw_path
    if candidate.is_file():
        return candidate
    return ROOT / raw_path


def _tool_names() -> set[str]:
    try:
        from trade_compass_agent.runtime.tools.operations import BUILTIN_OPERATION_TOOL_SCHEMAS
        from trade_compass_agent.runtime.tools.registry import BASE_TOOL_SCHEMAS, SCHEDULER_TOOL_SCHEMAS
        from trade_compass_agent.runtime.tools.readers import READER_TOOL_SCHEMAS
    except Exception as exc:
        _err(f"cannot import tool schemas: {type(exc).__name__}: {exc}")
        return set()
    names: set[str] = set()
    for schema in [*BASE_TOOL_SCHEMAS, *BUILTIN_OPERATION_TOOL_SCHEMAS, *SCHEDULER_TOOL_SCHEMAS, *READER_TOOL_SCHEMAS]:
        try:
            names.add(str(schema["function"]["name"]))
        except KeyError:
            continue
    return names


def _can_import(path: str) -> bool:
    module_path, _, attr = path.rpartition(":")
    if not module_path or not attr:
        return False
    try:
        module = importlib.import_module(module_path)
        getattr(module, attr)
    except Exception:
        return False
    return True


def _rel(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


if __name__ == "__main__":
    raise SystemExit(main())
