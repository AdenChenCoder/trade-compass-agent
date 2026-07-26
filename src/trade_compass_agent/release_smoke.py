"""Consumer-facing checks intended to run from an installed wheel."""

from __future__ import annotations

import json
from pathlib import Path

from trade_compass_agent import __version__
from trade_compass_agent.command_catalog import command_catalog
from trade_compass_agent.config import PACKAGE_ROOT, is_source_checkout, resolve_schema_path
from trade_compass_agent.runtime.skills import (
    AgentSkillsConfig,
    discover_external_skills,
    load_skill_reference,
)
from trade_compass_agent.web.dist import resolve_web_dist


def collect_release_smoke() -> dict[str, object]:
    if is_source_checkout():
        raise RuntimeError(
            "release smoke must run outside the source checkout against an installed wheel"
        )

    required = (
        PACKAGE_ROOT / "agent_skills.yaml",
        PACKAGE_ROOT / "default.yaml",
        PACKAGE_ROOT / "env.example",
        PACKAGE_ROOT / "workflows" / "catalyst_calendar_cn" / "workflow.yaml",
        PACKAGE_ROOT / "specialists" / "equity_research" / "specialist.yaml",
        resolve_schema_path("readers/reader_claims.schema.json"),
    )
    missing = [str(path) for path in required if not path.is_file()]
    web_dist = resolve_web_dist()
    if web_dist is None or not (web_dist / "index.html").is_file():
        missing.append("trade_compass_agent/web_dist/index.html")
    if missing:
        raise RuntimeError("installed package assets missing: " + ", ".join(missing))

    skills = discover_external_skills(
        project_root=Path("/__trade_compass_no_source_checkout__"),
        skills_config=AgentSkillsConfig(),
    )
    by_name = {skill.name: skill for skill in skills}
    expected_skills = {
        "catalyst-calendar-cn",
        "idea-generation-cn",
        "intraday-tech",
        "investment-masters",
    }
    missing_skills = sorted(expected_skills - by_name.keys())
    if missing_skills:
        raise RuntimeError("installed Skills missing: " + ", ".join(missing_skills))

    masters = by_name["investment-masters"]
    if not masters.description.startswith("五大投资大师"):
        raise RuntimeError("folded YAML Skill description was not parsed")
    reference = load_skill_reference(masters, "buffett")
    if reference.startswith('{"error"'):
        raise RuntimeError("bundled Skill reference could not be loaded")

    commands = command_catalog()
    if not any(item["command"] == "trade-compass data check" for item in commands):
        raise RuntimeError("canonical command catalog is incomplete")

    return {
        "version": __version__,
        "package_root": str(PACKAGE_ROOT),
        "skills": sorted(by_name),
        "commands": len(commands),
        "assets": "ok",
    }


def main() -> None:
    print(json.dumps(collect_release_smoke(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
