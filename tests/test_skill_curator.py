from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from trade_compass_agent.memory.skill_store import SkillStore
from trade_compass_agent.ops.curator import (
    DEFAULT_ARCHIVE_AFTER_DAYS,
    DEFAULT_STALE_AFTER_DAYS,
    run_curator,
)


_SKILL_TEMPLATE = """\
---
name: {name}
description: Test skill
category: test
---

## Steps
1. Do the thing for {name}
"""


def _create_agent_skill(store: SkillStore, name: str, *, days_old: int) -> None:
    result = store.create(name, _SKILL_TEMPLATE.format(name=name), created_by="agent")
    assert result["ok"] is True
    created_at = (datetime.now(timezone.utc) - timedelta(days=days_old)).isoformat()
    store._usage[name]["created_at"] = created_at
    store._save_usage()


def test_skill_curator_uses_fast_trading_lifecycle(tmp_path: Path) -> None:
    store = SkillStore(tmp_path / "skills")
    _create_agent_skill(store, "fresh-skill", days_old=DEFAULT_STALE_AFTER_DAYS - 1)
    _create_agent_skill(store, "stale-skill", days_old=DEFAULT_STALE_AFTER_DAYS + 1)
    _create_agent_skill(store, "archive-skill", days_old=DEFAULT_ARCHIVE_AFTER_DAYS + 1)

    actions = run_curator(store)

    assert actions["applied"] is False
    assert actions["stale"] == ["stale-skill"]
    assert actions["archived"] == ["archive-skill"]
    assert store.get("stale-skill").usage.state == "active"
    assert store.get("archive-skill") is not None

    actions = run_curator(store)

    assert actions["applied"] is True
    assert actions["stale"] == ["stale-skill"]
    assert actions["archived"] == ["archive-skill"]
    assert store.get("fresh-skill") is not None
    assert store.get("stale-skill").usage.state == "stale"
    assert store.get("archive-skill") is None
    assert (tmp_path / "skills" / ".archive" / "archive-skill" / "SKILL.md").is_file()


def test_skill_curator_keeps_pinned_old_skill(tmp_path: Path) -> None:
    store = SkillStore(tmp_path / "skills")
    _create_agent_skill(store, "pinned-skill", days_old=DEFAULT_ARCHIVE_AFTER_DAYS + 30)
    store.pin("pinned-skill")

    actions = run_curator(store)

    assert actions["archived"] == []
    assert store.get("pinned-skill") is not None


def test_skill_curator_reports_user_owned_without_mutation(tmp_path: Path) -> None:
    store = SkillStore(tmp_path / "skills")
    result = store.create("user-skill", _SKILL_TEMPLATE.format(name="user-skill"), created_by="user")
    assert result["ok"] is True
    store._usage["user-skill"]["created_at"] = (datetime.now(timezone.utc) - timedelta(days=99)).isoformat()
    store._save_usage()

    run_curator(store)
    actions = run_curator(store)

    assert actions["archived"] == []
    assert actions["user_owned_suggestions"]
    assert store.get("user-skill") is not None
    assert store.get("user-skill").usage.curator_managed is False
