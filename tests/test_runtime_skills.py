from __future__ import annotations

import json
from pathlib import Path

from trade_compass_agent.runtime.skills import (
    AgentSkillsConfig,
    SkillInfo,
    discover_external_skills,
    discover_skills,
    load_skill_reference,
)


def _write_skill(root: Path, name: str, frontmatter: str, body: str = "# Body") -> Path:
    skill_dir = root / ".trade-compass" / "skills" / name
    skill_dir.mkdir(parents=True)
    skill_md = skill_dir / "SKILL.md"
    skill_md.write_text(f"---\n{frontmatter}\n---\n\n{body}\n", encoding="utf-8")
    return skill_md


def test_discovery_parses_folded_yaml_description(tmp_path: Path) -> None:
    _write_skill(
        tmp_path,
        "folded-description",
        "name: folded-description\n"
        "description: >\n"
        "  First line of the trigger.\n"
        "  Second line of the trigger.",
    )

    skills = discover_external_skills(
        project_root=tmp_path,
        skills_config=AgentSkillsConfig(),
    )

    assert len(skills) == 1
    assert skills[0].description == ("First line of the trigger. Second line of the trigger.")


def test_discovery_falls_back_to_first_heading_for_invalid_frontmatter(
    tmp_path: Path,
) -> None:
    _write_skill(
        tmp_path,
        "fallback-description",
        "description: [invalid",
        body="# Reliable fallback heading\n\nDetails",
    )

    skills = discover_external_skills(
        project_root=tmp_path,
        skills_config=AgentSkillsConfig(),
    )

    assert skills[0].description == "Reliable fallback heading"


def test_reference_loader_rejects_parent_traversal(tmp_path: Path) -> None:
    skill_dir = tmp_path / "sample"
    references = skill_dir / "references"
    references.mkdir(parents=True)
    skill_md = skill_dir / "SKILL.md"
    skill_md.write_text("# Sample", encoding="utf-8")
    (references / "guide.md").write_text("safe reference", encoding="utf-8")
    skill = SkillInfo(
        name="sample",
        description="Sample",
        path=skill_md,
        source="project",
    )

    payload = json.loads(load_skill_reference(skill, "../SKILL"))

    assert "error" in payload
    assert payload["available"] == ["guide"]
    assert load_skill_reference(skill, "guide") == "safe reference"


def test_runtime_skill_overrides_same_named_builtin(tmp_path: Path) -> None:
    _write_skill(
        tmp_path,
        "shared-name",
        "name: shared-name\ndescription: Built-in description",
    )
    memory_dir = tmp_path / "memory"
    runtime_skill = memory_dir / "skills" / "shared-name"
    runtime_skill.mkdir(parents=True)
    (runtime_skill / "SKILL.md").write_text(
        "---\nname: shared-name\ndescription: Runtime description\n---\n\n# Runtime\n",
        encoding="utf-8",
    )

    skills = discover_skills(
        memory_dir=memory_dir,
        project_root=tmp_path,
        skills_config=AgentSkillsConfig(),
    )

    assert [(skill.name, skill.source) for skill in skills] == [("shared-name", "memory_vault")]
