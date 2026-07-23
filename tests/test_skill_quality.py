from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from trade_compass_agent.memory.skill_store import SkillStore
from trade_compass_agent.runtime.tools.registry import ToolRegistry
from trade_compass_agent.runtime.tools.self_improve import SKILL_MANAGE_SCHEMA, tool_skill_manage


def _skill(name: str, *, description: str = "Reusable test skill", body: str = "## Steps\n1. Check setup\n2. Emit result\n") -> str:
    return f"""\
---
name: {name}
description: {description}
category: test
---

{body}
"""


def test_user_foreground_skill_is_not_curator_managed(tmp_path: Path) -> None:
    store = SkillStore(tmp_path / "skills")

    result = json.loads(
        tool_skill_manage(
            store,
            "create",
            name="user-owned",
            content=_skill("user-owned"),
            actor="user",
        )
    )

    assert result["ok"] is True
    rec = store.get("user-owned")
    assert rec is not None
    assert rec.usage.created_by == "user"
    assert rec.usage.curator_managed is False
    assert "origin: user" in store.read_full("user-owned", record_view=False)


def test_agent_skill_quality_sidecar_and_list_fields(tmp_path: Path) -> None:
    store = SkillStore(tmp_path / "skills")
    result = store.create("agent-skill", _skill("agent-skill"), created_by="dreaming")

    assert result["ok"] is True
    quality_file = tmp_path / "skills" / "agent-skill" / ".quality.json"
    quality = json.loads(quality_file.read_text(encoding="utf-8"))
    assert "quality_score" not in quality
    assert quality["static_status"] in {"pass", "warning"}

    listed = json.loads(tool_skill_manage(store, "list"))
    item = listed["skills"][0]
    assert item["quality"]
    assert item["static_status"]
    assert "origin" not in item
    assert "actor" not in SKILL_MANAGE_SCHEMA["parameters"]["properties"]
    assert "quality: needs_patch" in store.read_full("agent-skill", record_view=False)


def test_quality_gate_rejects_unknown_tool_and_rolls_back(tmp_path: Path) -> None:
    store = SkillStore(tmp_path / "skills")
    bad = _skill("bad-skill", body="## Steps\n1. Run compute_not_real()\n")

    result = store.create("bad-skill", bad, created_by="agent")

    assert result["ok"] is False
    assert result["hard_errors"]
    assert store.get("bad-skill") is None


def test_quality_gate_warns_on_partial_overlap(tmp_path: Path) -> None:
    store = SkillStore(tmp_path / "skills")
    assert store.create("first-skill", _skill("first-skill"), created_by="agent")["ok"] is True
    second = _skill("second-skill", body="## Steps\n1. Check setup\n2. Emit result\n3. Note boundary\n")

    result = store.create("second-skill", second, created_by="agent")

    assert result["ok"] is True
    assert result["static_status"] == "warning"
    assert "first-skill" in result["warnings"][0]


def test_view_counts_and_quality_header(tmp_path: Path) -> None:
    store = SkillStore(tmp_path / "skills")
    assert store.create("view-skill", _skill("view-skill"), created_by="agent")["ok"] is True

    content = store.read_full("view-skill", with_quality_header=True)

    assert content and content.startswith("Quality:")
    assert store.get("view-skill").usage.view_count == 1

    store.record_use("view-skill")
    rec = store.get("view-skill")
    assert rec.usage.use_count == 1
    assert rec.usage.view_count == 2


def test_existing_skills_migrate_to_user_owned_and_lazy_quality(tmp_path: Path) -> None:
    skill_dir = tmp_path / "skills" / "legacy-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(_skill("legacy-skill"), encoding="utf-8")
    (tmp_path / "skills" / ".usage.json").write_text(
        json.dumps({"legacy-skill": {"created_by": "agent"}}),
        encoding="utf-8",
    )

    store = SkillStore(tmp_path / "skills")
    rec = store.get("legacy-skill")

    assert rec is not None
    assert rec.usage.created_by == "user"
    assert rec.usage.curator_managed is False
    assert (skill_dir / ".quality.json").is_file()
    assert "origin: user" in store.read_full("legacy-skill", record_view=False)


def test_existing_curator_managed_flag_is_preserved(tmp_path: Path) -> None:
    skill_dir = tmp_path / "skills" / "auto-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(_skill("auto-skill"), encoding="utf-8")
    (tmp_path / "skills" / ".usage.json").write_text(
        json.dumps({"auto-skill": {"created_by": "agent", "curator_managed": True}}),
        encoding="utf-8",
    )

    store = SkillStore(tmp_path / "skills")

    assert store.get("auto-skill").usage.curator_managed is True


def test_registry_foreground_skill_create_is_user_owned(tmp_path: Path) -> None:
    stack = SimpleNamespace(config=SimpleNamespace(memory_dir=tmp_path / "vault"))
    store = SkillStore(tmp_path / "vault" / "skills")
    registry = ToolRegistry(stack, skill_store=store)

    result = json.loads(
        registry.execute(
            "skill_manage",
            {
                "action": "create",
                "name": "foreground-skill",
                "content": _skill("foreground-skill"),
                "actor": "agent",
            },
        )
    )

    assert result["ok"] is True
    rec = store.get("foreground-skill")
    assert rec.usage.created_by == "user"
    assert rec.usage.curator_managed is False


def test_registry_scheduled_skill_create_is_curator_managed(tmp_path: Path) -> None:
    stack = SimpleNamespace(config=SimpleNamespace(memory_dir=tmp_path / "vault"))
    store = SkillStore(tmp_path / "vault" / "skills")
    registry = ToolRegistry(stack, skill_store=store, memory_actor="scheduler", skill_actor="scheduler")

    result = json.loads(
        registry.execute(
            "skill_manage",
            {
                "action": "create",
                "name": "scheduled-skill",
                "content": _skill("scheduled-skill"),
            },
        )
    )

    assert result["ok"] is True
    rec = store.get("scheduled-skill")
    assert rec.usage.created_by == "scheduler"
    assert rec.usage.curator_managed is True


def test_dreaming_skill_create_keeps_dreaming_origin(tmp_path: Path) -> None:
    store = SkillStore(tmp_path / "skills")

    result = json.loads(
        tool_skill_manage(
            store,
            "create",
            name="dreaming-skill",
            content=_skill("dreaming-skill"),
            actor="dreaming",
        )
    )

    assert result["ok"] is True
    rec = store.get("dreaming-skill")
    assert rec.usage.created_by == "dreaming"
    assert rec.usage.curator_managed is True
    assert "origin: dreaming" in store.read_full("dreaming-skill", record_view=False)
