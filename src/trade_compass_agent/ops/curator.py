"""Curator — periodic skill library maintenance.

Runs when idle (>= 2h) at weekly intervals:
1. Auto-transitions: stale (7d) → archived (21d), no LLM needed
2. Performance-based archival: win_rate < 30% after 20+ signals → archive
3. (Future) LLM consolidation pass for overlapping skills

Integration: called from scheduler or CLI.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
import json

from trade_compass_agent.memory.skill_store import SkillStore

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_HOURS = 168  # 7 days
DEFAULT_MIN_IDLE_HOURS = 2
DEFAULT_STALE_AFTER_DAYS = 7
DEFAULT_ARCHIVE_AFTER_DAYS = 21


def should_run_curator(
    last_run_at: datetime | None,
    interval_hours: int = DEFAULT_INTERVAL_HOURS,
) -> bool:
    """Check if curator should run based on interval."""
    if last_run_at is None:
        return False  # first run deferred one interval
    elapsed = (datetime.now(timezone.utc) - last_run_at).total_seconds() / 3600
    return elapsed >= interval_hours


def run_curator(skill_store: SkillStore) -> dict:
    """Run curator maintenance pass.

    Returns summary of actions taken.
    """
    state_file = skill_store._dir / ".curator_state"
    first_run = not state_file.is_file()
    actions = {
        "stale": [],
        "archived": [],
        "reactivated": [],
        "consolidations": [],
        "prunings": [],
        "user_owned_suggestions": [],
        "warnings": [],
        "applied": not first_run,
    }
    now = datetime.now(timezone.utc)

    for skill in skill_store.user_owned_skills():
        if skill.quality.static_status != "pass" or skill.usage.state == "stale":
            actions["user_owned_suggestions"].append(
                {
                    "name": skill.name,
                    "state": skill.usage.state,
                    "quality": skill.quality.quality,
                    "static_status": skill.quality.static_status,
                    "reason": skill.quality.needs_patch_reason,
                }
            )

    for skill in skill_store.curator_managed_skills():
        if skill.usage.pinned:
            continue

        last_activity = skill.usage.last_used_at or skill.usage.last_patched_at or skill.usage.created_at
        if not last_activity:
            continue

        try:
            anchor = datetime.fromisoformat(last_activity.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            continue

        days_inactive = (now - anchor).days

        if days_inactive >= DEFAULT_ARCHIVE_AFTER_DAYS:
            if actions["applied"]:
                result = skill_store.archive(skill.name)
            else:
                result = {"ok": True}
            if result.get("ok"):
                actions["archived"].append(skill.name)
                logger.info("Curator archived skill: %s (inactive %d days)", skill.name, days_inactive)
        elif days_inactive >= DEFAULT_STALE_AFTER_DAYS and skill.usage.state == "active":
            if actions["applied"]:
                skill_store.mark_stale(skill.name)
            actions["stale"].append(skill.name)
            logger.info("Curator marked stale: %s (inactive %d days)", skill.name, days_inactive)
        elif days_inactive < DEFAULT_STALE_AFTER_DAYS and skill.usage.state == "stale":
            actions["reactivated"].append(skill.name)

    state_file.write_text(
        json.dumps({"last_run_at": now.isoformat(), "report_only": first_run}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    total = sum(len(v) for k, v in actions.items() if isinstance(v, list) and k != "warnings")
    logger.info("Curator pass complete: %d actions", total)
    return actions
