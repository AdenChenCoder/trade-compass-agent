"""Skill store — CRUD operations, usage tracking, and lifecycle management.

Skills are procedural memory: "how to do a class of task."
Each skill is a directory with SKILL.md + optional references/templates/scripts/.

Lifecycle: active → stale (7d unused) → archived (21d unused)
"""

from __future__ import annotations

import json
import logging
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from trade_compass_agent.memory.skill_quality import (
    SkillQuality,
    evaluate_skill_content,
    normalize_skill_content,
    parse_skill_frontmatter,
    read_quality_file,
    update_skill_frontmatter,
    write_quality_file,
)

logger = logging.getLogger(__name__)

_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_MAX_NAME_LENGTH = 64
_MAX_CONTENT_CHARS = 100_000
_STALE_AFTER_DAYS = 7
_ARCHIVE_AFTER_DAYS = 21


@dataclass
class SkillUsage:
    created_by: str | None = None
    curator_managed: bool = True
    use_count: int = 0
    view_count: int = 0
    patch_count: int = 0
    last_used_at: str | None = None
    last_viewed_at: str | None = None
    last_patched_at: str | None = None
    created_at: str | None = None
    state: str = "active"
    pinned: bool = False


@dataclass
class SkillRecord:
    name: str
    description: str
    category: str
    path: Path
    usage: SkillUsage = field(default_factory=SkillUsage)
    quality: SkillQuality = field(default_factory=SkillQuality)


class SkillStore:
    """Manages trading skills with usage tracking and lifecycle."""

    def __init__(self, skills_dir: Path) -> None:
        self._dir = skills_dir
        self._archive_dir = skills_dir / ".archive"
        self._usage_file = skills_dir / ".usage.json"
        skills_dir.mkdir(parents=True, exist_ok=True)
        self._archive_dir.mkdir(exist_ok=True)
        self._usage: dict[str, dict] = self._load_usage()
        self._migrate_usage()

    def list_skills(self, include_stale: bool = False) -> list[SkillRecord]:
        """List all active skills."""
        results = []
        for skill_md in sorted(self._dir.glob("*/SKILL.md")):
            name = skill_md.parent.name
            if name.startswith("."):
                continue
            usage = self._get_usage(name)
            if not include_stale and usage.state == "stale":
                continue
            meta = self._parse_frontmatter(skill_md)
            results.append(SkillRecord(
                name=name,
                description=meta.get("description", ""),
                category=meta.get("category", "general"),
                path=skill_md,
                usage=usage,
                quality=self._get_quality(name),
            ))
        return results

    def get(self, name: str) -> SkillRecord | None:
        """Get a specific skill."""
        path = self._dir / name / "SKILL.md"
        if not path.is_file():
            return None
        usage = self._get_usage(name)
        meta = self._parse_frontmatter(path)
        return SkillRecord(
            name=name,
            description=meta.get("description", ""),
            category=meta.get("category", "general"),
            path=path,
            usage=usage,
            quality=self._get_quality(name),
        )

    def get_by_category(self, category: str) -> list[SkillRecord]:
        """Get skills filtered by category."""
        return [s for s in self.list_skills() if s.category == category]

    def read_full(self, name: str, *, record_view: bool = True, with_quality_header: bool = False) -> str | None:
        """Read full SKILL.md content."""
        path = self._dir / name / "SKILL.md"
        if not path.is_file():
            return None
        if record_view:
            self._bump_view(name)
        content = path.read_text(encoding="utf-8")
        if with_quality_header:
            return self.format_quality_header(name) + content
        return content

    def record_use(self, name: str) -> None:
        """Record that a skill was actively used by the agent."""
        self._bump_view(name)
        self._bump_use(name)

    def create(self, name: str, content: str, *, created_by: str = "agent") -> dict[str, Any]:
        """Create a new skill."""
        if not _NAME_PATTERN.match(name):
            return {"ok": False, "error": "Invalid name. Use lowercase alphanumeric + .-_"}
        if len(name) > _MAX_NAME_LENGTH:
            return {"ok": False, "error": f"Name too long (max {_MAX_NAME_LENGTH})"}
        if len(content) > _MAX_CONTENT_CHARS:
            return {"ok": False, "error": f"Content too large (max {_MAX_CONTENT_CHARS} chars)"}

        skill_dir = self._dir / name
        if skill_dir.exists():
            return {"ok": False, "error": f"Skill '{name}' already exists. Use patch to update."}

        if not content.startswith("---"):
            return {"ok": False, "error": "SKILL.md must start with YAML frontmatter (---\\nname: ...\\n---)"}

        skill_dir.mkdir(parents=True)
        content = normalize_skill_content(
            content,
            name=name,
            origin=created_by,
            quality="draft",
            evidence_count=0,
        )
        (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")

        now = datetime.now(timezone.utc).isoformat()
        self._usage[name] = {
            "created_by": created_by,
            "curator_managed": self._is_curator_managed(created_by),
            "use_count": 0,
            "view_count": 0,
            "patch_count": 0,
            "last_used_at": None,
            "last_viewed_at": None,
            "last_patched_at": None,
            "created_at": now,
            "state": "active",
            "pinned": False,
        }
        self._save_usage()
        quality = self.review_quality(name)
        if quality.static_status == "fail":
            shutil.rmtree(skill_dir, ignore_errors=True)
            self._usage.pop(name, None)
            self._save_usage()
        return {
            "ok": quality.static_status != "fail",
            "path": str(skill_dir / "SKILL.md"),
            "quality": quality.quality,
            "static_status": quality.static_status,
            "warnings": quality.warnings,
            "hard_errors": quality.hard_errors,
        }

    def patch(self, name: str, old_text: str, new_text: str) -> dict[str, Any]:
        """Patch a skill (fuzzy find and replace)."""
        path = self._dir / name / "SKILL.md"
        if not path.is_file():
            return {"ok": False, "error": f"Skill '{name}' not found"}

        usage = self._get_usage(name)
        if usage.pinned and usage.created_by != "agent":
            return {"ok": False, "error": f"Skill '{name}' is pinned (user-owned)"}

        content = path.read_text(encoding="utf-8")
        if old_text not in content:
            return {"ok": False, "error": f"Text not found in {name}/SKILL.md"}

        updated = content.replace(old_text, new_text, 1)
        path.write_text(updated, encoding="utf-8")
        quality = self.review_quality(name)
        if quality.static_status == "fail":
            failed = quality
            path.write_text(content, encoding="utf-8")
            self.review_quality(name)
            return {"ok": False, "error": "quality gate failed; patch rolled back",
                    "quality": failed.quality, "static_status": failed.static_status,
                    "warnings": failed.warnings, "hard_errors": failed.hard_errors}
        self._bump_patch(name)
        quality = self.review_quality(name)
        return {"ok": quality.static_status != "fail", "quality": quality.quality, "static_status": quality.static_status,
                "warnings": quality.warnings, "hard_errors": quality.hard_errors}

    def edit(self, name: str, new_content: str) -> dict[str, Any]:
        """Full rewrite of SKILL.md."""
        path = self._dir / name / "SKILL.md"
        if not path.is_file():
            return {"ok": False, "error": f"Skill '{name}' not found"}
        if not new_content.startswith("---"):
            return {"ok": False, "error": "Must include YAML frontmatter"}
        if len(new_content) > _MAX_CONTENT_CHARS:
            return {"ok": False, "error": "Content too large"}

        usage = self._get_usage(name)
        new_content = normalize_skill_content(
            new_content,
            name=name,
            origin=usage.created_by or "agent",
            quality="draft",
            evidence_count=0,
        )
        old_content = path.read_text(encoding="utf-8")
        path.write_text(new_content, encoding="utf-8")
        quality = self.review_quality(name)
        if quality.static_status == "fail":
            failed = quality
            path.write_text(old_content, encoding="utf-8")
            self.review_quality(name)
            return {"ok": False, "error": "quality gate failed; edit rolled back",
                    "quality": failed.quality, "static_status": failed.static_status,
                    "warnings": failed.warnings, "hard_errors": failed.hard_errors}
        self._bump_patch(name)
        quality = self.review_quality(name)
        return {"ok": quality.static_status != "fail", "quality": quality.quality, "static_status": quality.static_status,
                "warnings": quality.warnings, "hard_errors": quality.hard_errors}

    def archive(self, name: str) -> dict[str, Any]:
        """Move skill to .archive/ (recoverable)."""
        skill_dir = self._dir / name
        if not skill_dir.is_dir():
            return {"ok": False, "error": f"Skill '{name}' not found"}

        usage = self._get_usage(name)
        if usage.pinned:
            return {"ok": False, "error": f"Skill '{name}' is pinned, cannot archive"}

        dest = self._archive_dir / name
        if dest.exists():
            shutil.rmtree(dest)
        shutil.move(str(skill_dir), str(dest))

        if name in self._usage:
            self._usage[name]["state"] = "archived"
            self._usage[name]["archived_at"] = datetime.now(timezone.utc).isoformat()
            self._save_usage()
        return {"ok": True}

    def restore(self, name: str) -> dict[str, Any]:
        """Restore an archived skill."""
        src = self._archive_dir / name
        if not src.is_dir():
            return {"ok": False, "error": f"No archived skill '{name}'"}
        dest = self._dir / name
        if dest.exists():
            return {"ok": False, "error": f"Active skill '{name}' already exists"}
        shutil.move(str(src), str(dest))
        if name in self._usage:
            self._usage[name]["state"] = "active"
            self._save_usage()
        return {"ok": True}

    def pin(self, name: str) -> dict[str, Any]:
        """Pin a skill (protected from curator)."""
        if name not in self._usage:
            self._usage[name] = {}
        self._usage[name]["pinned"] = True
        self._save_usage()
        return {"ok": True}

    def unpin(self, name: str) -> dict[str, Any]:
        """Unpin a skill."""
        if name in self._usage:
            self._usage[name]["pinned"] = False
            self._save_usage()
        return {"ok": True}

    def agent_created_skills(self) -> list[SkillRecord]:
        """Skills eligible for curator maintenance."""
        return self.curator_managed_skills()

    def curator_managed_skills(self) -> list[SkillRecord]:
        """Skills that automated curator is allowed to mutate."""
        return [s for s in self.list_skills(include_stale=True) if s.usage.curator_managed]

    def user_owned_skills(self) -> list[SkillRecord]:
        """Skills that curator may report on but must not mutate."""
        return [s for s in self.list_skills(include_stale=True) if not s.usage.curator_managed]

    def mark_stale(self, name: str) -> None:
        if name in self._usage:
            self._usage[name]["state"] = "stale"
            self._save_usage()

    def review_quality(self, name: str) -> SkillQuality:
        path = self._dir / name / "SKILL.md"
        if not path.is_file():
            return SkillQuality(static_status="fail", hard_errors=[f"Skill '{name}' not found"])
        existing = {
            skill_md.parent.name: skill_md.read_text(encoding="utf-8")
            for skill_md in sorted(self._dir.glob("*/SKILL.md"))
            if not skill_md.parent.name.startswith(".")
        }
        quality = evaluate_skill_content(
            name=name,
            content=path.read_text(encoding="utf-8"),
            existing=existing,
            usage=self._get_usage(name),
        )
        self._sync_frontmatter_quality(path, quality)
        write_quality_file(path.parent, quality)
        return quality

    def format_quality_header(self, name: str) -> str:
        quality = self._get_quality(name)
        warnings = ", ".join(quality.warnings) if quality.warnings else "none"
        return f"Quality: {quality.quality}\nStatic status: {quality.static_status}\nWarnings: {warnings}\n\n"

    def _get_usage(self, name: str) -> SkillUsage:
        raw = self._usage.get(name, {})
        return SkillUsage(
            created_by=raw.get("created_by"),
            curator_managed=raw.get("curator_managed", self._is_curator_managed(raw.get("created_by"))),
            use_count=raw.get("use_count", 0),
            view_count=raw.get("view_count", 0),
            patch_count=raw.get("patch_count", 0),
            last_used_at=raw.get("last_used_at"),
            last_viewed_at=raw.get("last_viewed_at"),
            last_patched_at=raw.get("last_patched_at"),
            created_at=raw.get("created_at"),
            state=raw.get("state", "active"),
            pinned=raw.get("pinned", False),
        )

    def _bump_use(self, name: str) -> None:
        if name not in self._usage:
            self._usage[name] = {}
        self._usage[name]["use_count"] = self._usage[name].get("use_count", 0) + 1
        self._usage[name]["last_used_at"] = datetime.now(timezone.utc).isoformat()
        self._save_usage()

    def _bump_view(self, name: str) -> None:
        if name not in self._usage:
            self._usage[name] = {}
        self._usage[name]["view_count"] = self._usage[name].get("view_count", 0) + 1
        self._usage[name]["last_viewed_at"] = datetime.now(timezone.utc).isoformat()
        self._save_usage()

    def _bump_patch(self, name: str) -> None:
        if name not in self._usage:
            self._usage[name] = {}
        self._usage[name]["patch_count"] = self._usage[name].get("patch_count", 0) + 1
        self._usage[name]["last_patched_at"] = datetime.now(timezone.utc).isoformat()
        if self._usage[name].get("state") == "stale":
            self._usage[name]["state"] = "active"
        self._save_usage()

    def _load_usage(self) -> dict[str, dict]:
        if not self._usage_file.is_file():
            return {}
        try:
            return json.loads(self._usage_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}

    def _migrate_usage(self) -> None:
        """Backfill ownership fields without trusting source as quality."""
        changed = False
        for skill_md in sorted(self._dir.glob("*/SKILL.md")):
            name = skill_md.parent.name
            if name.startswith("."):
                continue
            raw = self._usage.setdefault(name, {})
            legacy_without_policy = "curator_managed" not in raw
            if legacy_without_policy:
                raw["created_by"] = "user"
                changed = True
            if "created_by" not in raw or not raw.get("created_by"):
                raw["created_by"] = "user"
                changed = True
            if legacy_without_policy:
                raw["curator_managed"] = False
                changed = True
            if "view_count" not in raw:
                raw["view_count"] = 0
                changed = True
            content = skill_md.read_text(encoding="utf-8")
            meta, _body, error = parse_skill_frontmatter(content)
            origin = str(meta.get("origin") or raw.get("created_by") or "user") if not error else raw.get("created_by", "user")
            normalized = normalize_skill_content(
                content,
                name=name,
                origin=origin,
                quality=str(meta.get("quality") or "active") if not error else "active",
                evidence_count=int(meta.get("evidence_count") or 0) if not error else 0,
            )
            if normalized != content:
                skill_md.write_text(normalized, encoding="utf-8")
                changed = True
            quality_path = skill_md.parent / ".quality.json"
            if not quality_path.is_file():
                self.review_quality(name)
        if changed:
            self._save_usage()

    def _save_usage(self) -> None:
        self._usage_file.write_text(
            json.dumps(self._usage, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    def _get_quality(self, name: str) -> SkillQuality:
        quality_path = self._dir / name / ".quality.json"
        if not quality_path.is_file() and (self._dir / name / "SKILL.md").is_file():
            return self.review_quality(name)
        return read_quality_file(self._dir / name)

    @staticmethod
    def _is_curator_managed(created_by: str | None) -> bool:
        return created_by in {"agent", "background_review", "scheduler", "dreaming"}

    @staticmethod
    def _sync_frontmatter_quality(path: Path, quality: SkillQuality) -> None:
        content = path.read_text(encoding="utf-8")
        updated = update_skill_frontmatter(
            content,
            {
                "quality": quality.quality,
                "evidence_count": quality.evidence_count,
            },
        )
        if updated != content:
            path.write_text(updated, encoding="utf-8")

    def _parse_frontmatter(self, path: Path) -> dict[str, str]:
        text = path.read_text(encoding="utf-8")
        meta, _body, error = parse_skill_frontmatter(text)
        if error:
            return {}
        return {str(k).strip().lower(): str(v).strip() for k, v in meta.items()}
