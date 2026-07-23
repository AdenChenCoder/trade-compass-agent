from __future__ import annotations

import json

from trade_compass_agent.runtime.skills import discover_skills, load_agent_skills_config, load_skill_body, load_skill_reference


def tool_load_skill(*, memory_dir, name: str, reference: str | None = None) -> str:
    skills_cfg = load_agent_skills_config()
    skills = discover_skills(memory_dir=memory_dir, skills_config=skills_cfg)
    for skill in skills:
        if skill.name == name:
            if reference:
                return load_skill_reference(skill, reference)
            return load_skill_body(skill)
    names = [s.name for s in skills]
    return json.dumps(
        {"error": f"skill not found: {name}", "available": names},
        ensure_ascii=False,
    )
