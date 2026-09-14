from __future__ import annotations

import json

from trade_compass_agent.runtime.skills import AgentSkillsConfig, discover_skills, load_agent_skills_config, load_skill_body, load_skill_reference
from trade_compass_agent.concurrency import file_transaction


def tool_load_skill(*, memory_dir, name: str, reference: str | None = None) -> str:
    with file_transaction(memory_dir / "skills" / ".skills.lock"):
        return _load_skill(memory_dir=memory_dir, name=name, reference=reference)


def _load_skill(*, memory_dir, name, reference):
    skills_cfg = load_agent_skills_config()
    skills = discover_skills(memory_dir=memory_dir, skills_config=skills_cfg)
    for skill in skills:
        if skill.name == name:
            if reference:
                return load_skill_reference(skill, reference)
            return load_skill_body(skill)
    all_skills = discover_skills(memory_dir=memory_dir, skills_config=AgentSkillsConfig())
    reason = "disabled" if any(s.name == name for s in all_skills) else "not_found"
    usage_path = memory_dir / "skills" / ".usage.json"
    usage = json.loads(usage_path.read_text()) if usage_path.is_file() else {}
    if usage.get(name, {}).get("state") == "archived":
        reason = "archived"
    names = [s.name for s in skills]
    return json.dumps(
        {"error": f"skill {reason}: {name}", "disposition": reason, "available": names},
        ensure_ascii=False,
    )
