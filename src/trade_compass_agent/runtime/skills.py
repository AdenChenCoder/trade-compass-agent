from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from trade_compass_agent.config import PACKAGE_ROOT, PROJECT_ROOT, is_source_checkout
from trade_compass_agent.memory.skill_quality import parse_skill_frontmatter, read_quality_file

SOURCE_AGENT_SKILLS_CONFIG_PATH = PROJECT_ROOT / "config" / "agent_skills.yaml"
PACKAGED_AGENT_SKILLS_CONFIG_PATH = PACKAGE_ROOT / "agent_skills.yaml"
AGENT_SKILLS_CONFIG_PATH = (
    SOURCE_AGENT_SKILLS_CONFIG_PATH if is_source_checkout() else PACKAGED_AGENT_SKILLS_CONFIG_PATH
)
BUILTIN_SKILLS_ROOT = PACKAGE_ROOT / "builtin_skills"


@dataclass(frozen=True)
class SkillInfo:
    name: str
    description: str
    path: Path
    source: str


@dataclass(frozen=True)
class AgentSkillsConfig:
    enabled_skills: frozenset[str] | None = None
    default_summaries: dict[str, str] = field(default_factory=dict)
    pinned: tuple[str, ...] = ()


def load_agent_skills_config(path: Path | None = None) -> AgentSkillsConfig:
    config_path = path or AGENT_SKILLS_CONFIG_PATH
    if not config_path.is_file():
        return AgentSkillsConfig()
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    enabled_raw = raw.get("enabled_skills")
    enabled: frozenset[str] | None = None
    if enabled_raw is not None:
        enabled = frozenset(str(name).strip() for name in enabled_raw if str(name).strip())
    summaries_raw = raw.get("default_summaries") or {}
    summaries = {
        str(name).strip(): str(text).strip()
        for name, text in summaries_raw.items()
        if str(name).strip() and str(text).strip()
    }
    pinned_raw = raw.get("pinned") or []
    pinned = tuple(str(name).strip() for name in pinned_raw if str(name).strip())
    return AgentSkillsConfig(
        enabled_skills=enabled,
        default_summaries=summaries,
        pinned=pinned,
    )


def apply_skills_config(
    skills: list[SkillInfo],
    config: AgentSkillsConfig,
) -> list[SkillInfo]:
    """Filter and order skills. Config only controls external (project) skills;
    agent self-evolved skills (memory_vault) are always included."""
    filtered = skills
    if config.enabled_skills is not None:
        enabled = set(config.enabled_skills)
        filtered = [
            skill for skill in skills if skill.source == "memory_vault" or skill.name in enabled
        ]
    if not config.pinned:
        return filtered
    pinned_names = set(config.pinned)
    pinned_order = {name: index for index, name in enumerate(config.pinned)}
    pinned = sorted(
        [skill for skill in filtered if skill.name in pinned_names],
        key=lambda skill: pinned_order.get(skill.name, 999),
    )
    rest = [skill for skill in filtered if skill.name not in pinned_names]
    return pinned + rest


def discover_external_skills(
    *,
    project_root: Path = PROJECT_ROOT,
    skills_config: AgentSkillsConfig | None = None,
) -> list[SkillInfo]:
    """Discover only external (project-level) skills for settings display."""
    root = _external_skills_root(project_root)
    found: list[SkillInfo] = []
    if root.is_dir():
        for skill_md in sorted(root.glob("*/SKILL.md")):
            name, description = _parse_skill_md(skill_md)
            found.append(
                SkillInfo(name=name, description=description, path=skill_md, source="project")
            )
    config = skills_config or load_agent_skills_config()
    if config.enabled_skills is not None:
        enabled = set(config.enabled_skills)
        found = [s for s in found if s.name in enabled]
    return found


def _parse_skill_md(path: Path) -> tuple[str, str]:
    text = path.read_text(encoding="utf-8")
    name = path.parent.name
    metadata, body, _ = parse_skill_frontmatter(text)
    raw_description = metadata.get("description")
    description = str(raw_description).strip() if raw_description is not None else ""
    if not description:
        first = next((ln.strip() for ln in body.splitlines() if ln.strip()), "")
        description = first.lstrip("#").strip()[:200]
    return name, description


def discover_skills(
    *,
    memory_dir: Path,
    project_root: Path = PROJECT_ROOT,
    skills_config: AgentSkillsConfig | None = None,
) -> list[SkillInfo]:
    # Built-ins establish defaults; writable runtime Skills intentionally
    # override a built-in with the same directory/name.
    roots: list[tuple[str, Path]] = [
        ("project", _external_skills_root(project_root)),
        ("memory_vault", memory_dir / "skills"),
    ]
    found: dict[str, SkillInfo] = {}
    for source, root in roots:
        if not root.is_dir():
            continue
        for skill_md in sorted(root.glob("*/SKILL.md")):
            name, description = _parse_skill_md(skill_md)
            found[name] = SkillInfo(
                name=name,
                description=description,
                path=skill_md,
                source=source,
            )
    config = skills_config or load_agent_skills_config()
    return apply_skills_config(list(found.values()), config)


def _external_skills_root(project_root: Path) -> Path:
    """Use editable project skills in source checkouts and bundled skills in wheels."""
    project_skills = project_root / ".trade-compass" / "skills"
    return project_skills if project_skills.is_dir() else BUILTIN_SKILLS_ROOT


def load_skill_body(skill: SkillInfo) -> str:
    body = skill.path.read_text(encoding="utf-8")
    quality = read_quality_file(skill.path.parent)
    warnings = ", ".join(quality.warnings) if quality.warnings else "none"
    header = f"Quality: {quality.quality}\nStatic status: {quality.static_status}\nWarnings: {warnings}\n\n"
    return header + body


def load_skill_reference(skill: SkillInfo, reference: str) -> str:
    """Load a reference sub-document from the skill's references/ directory."""
    import json as _json

    ref_dir = skill.path.parent / "references"
    if not ref_dir.is_dir():
        return _json.dumps(
            {"error": f"skill '{skill.name}' has no references/ directory"},
            ensure_ascii=False,
        )
    if (
        not reference
        or Path(reference).name != reference
        or reference in {".", ".."}
        or reference.endswith(".md")
    ):
        return _json.dumps(
            {
                "error": "reference must be a file name without path separators or .md",
                "available": [p.stem for p in sorted(ref_dir.glob("*.md"))],
            },
            ensure_ascii=False,
        )
    ref_path = ref_dir / f"{reference}.md"
    if not ref_path.is_file():
        available = [p.stem for p in sorted(ref_dir.glob("*.md"))]
        return _json.dumps(
            {"error": f"reference '{reference}' not found", "available": available},
            ensure_ascii=False,
        )
    return ref_path.read_text(encoding="utf-8")


def skills_summary(
    skills: list[SkillInfo],
    *,
    max_chars: int = 4000,
    summary_overrides: dict[str, str] | None = None,
) -> str:
    overrides = summary_overrides or {}
    lines = ["Available skills (use load_skill for full text):"]
    for skill in skills:
        description = overrides.get(skill.name, skill.description)
        lines.append(f"- {skill.name} ({skill.source}): {description}")
    text = "\n".join(lines)
    if len(text) > max_chars:
        return text[: max_chars - 3] + "..."
    return text
