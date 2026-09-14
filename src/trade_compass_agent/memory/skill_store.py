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
import hashlib
import base64
import threading
from contextlib import contextmanager
from functools import wraps
from dataclasses import asdict

from trade_compass_agent.concurrency import atomic_write, file_transaction
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from trade_compass_agent.memory.skill_quality import (
    SkillQuality,
    evaluate_skill_content,
    normalize_skill_content,
    parse_skill_frontmatter,
    update_skill_frontmatter,
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
    source: str = "memory_vault"
    enabled: bool = True
    version: str = ""



def recover_skill_transaction(skills_dir: Path):
    """Replay a validated, durable commit before any consumer reads its files."""
    journal = skills_dir / ".skill-transaction.json"
    if not journal.is_file():
        return
    record = json.loads(journal.read_text(encoding="utf-8"))
    for relative, encoded in record["writes"].items():
        path = (skills_dir / relative).resolve()
        if not path.is_relative_to(skills_dir.resolve()):
            raise ValueError("Invalid skill transaction path")
        path.parent.mkdir(parents=True, exist_ok=True)
        # Resources may be binary. Atomic replacement preserves complete versions.
        import os
        import tempfile
        fd, temporary = tempfile.mkstemp(dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(base64.b64decode(encoded))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    for relative in record.get("remove", []):
        path = (skills_dir / relative).resolve()
        if not path.is_relative_to(skills_dir.resolve()) or path == skills_dir.resolve():
            raise ValueError("Invalid skill transaction removal")
        if path.is_dir():
            shutil.rmtree(path)
        elif path.is_file():
            path.unlink()
    journal.unlink()


def _locked(method):
    @wraps(method)
    def call(self, *args, **kwargs):
        with self._transaction():
            return method(self, *args, **kwargs)
    return call


class SkillStore:
    """Manages trading skills with usage tracking and lifecycle."""

    def __init__(self, skills_dir: Path) -> None:
        self._dir = skills_dir
        self._archive_dir = skills_dir / ".archive"
        self._usage_file = skills_dir / ".usage.json"
        skills_dir.mkdir(parents=True, exist_ok=True)
        self._archive_dir.mkdir(exist_ok=True)
        self._local = threading.local()
        self._usage: dict[str, dict] = {}
        with self._transaction():
            self._migrate_usage()

    @contextmanager
    def _transaction(self):
        with file_transaction(self._dir / ".skills.lock"):
            outer = not getattr(self._local, "depth", 0)
            self._local.depth = getattr(self._local, "depth", 0) + 1
            try:
                if outer:
                    recover_skill_transaction(self._dir)
                    self._usage = self._load_usage()
                yield
            finally:
                self._local.depth -= 1

    def _catalog(self, *, all_skills=True):
        from trade_compass_agent.runtime.skills import discover_skills, AgentSkillsConfig
        return {s.name: s for s in discover_skills(memory_dir=self._dir.parent,
                skills_config=AgentSkillsConfig() if all_skills else None)}

    def _path(self, name):
        if not _NAME_PATTERN.fullmatch(name) or len(name) > _MAX_NAME_LENGTH:
            return None
        skill = self._catalog().get(name)
        return skill.path if skill else None

    @staticmethod
    def _version(path):
        # The basis covers reference resources too, not just the main body.
        digest = hashlib.sha256()
        for file in sorted(path.parent.rglob("*")):
            if file.is_file() and not any(p.startswith(".") for p in file.relative_to(path.parent).parts):
                digest.update(str(file.relative_to(path.parent)).encode())
                digest.update(file.read_bytes())
        return digest.hexdigest()[:20]

    @_locked
    def version(self, name):
        path = self._path(name)
        return self._version(path) if path else None

    @_locked
    def list_skills(self, include_stale=False):
        enabled = self._catalog(all_skills=False)
        result = []
        for name, skill in sorted(self._catalog().items()):
            usage = self._get_usage(name)
            if usage.state == "archived" or (not include_stale and usage.state == "stale"):
                continue
            meta = self._parse_frontmatter(skill.path)
            result.append(SkillRecord(name, meta.get("description", ""), meta.get("category", "general"),
                          skill.path, usage, self._get_quality(name), skill.source, name in enabled, self._version(skill.path)))
        return result

    @_locked
    def get(self, name):
        path = self._path(name)
        if path is None:
            return None
        meta = self._parse_frontmatter(path)
        skill = self._catalog()[name]
        return SkillRecord(name, meta.get("description", ""), meta.get("category", "general"), path,
                           self._get_usage(name), self._get_quality(name), skill.source,
                           name in self._catalog(all_skills=False), self._version(path))

    def get_by_category(self, category: str) -> list[SkillRecord]:
        """Get skills filtered by category."""
        return [s for s in self.list_skills() if s.category == category]

    @_locked
    def view(self, name):
        path = self._path(name)
        if path is None:
            state = "archived" if self._get_usage(name).state == "archived" else "not_found"
            return {"ok": False, "disposition": state, "error": f"Skill '{name}' {state}"}
        self._bump_view(name)
        skill = self._catalog()[name]
        return {"ok": True, "changed": False, "name": name, "content": path.read_text(encoding="utf-8"),
                "version": self._version(path), "source": skill.source, "enabled": name in self._catalog(all_skills=False)}

    @_locked
    def read_full(self, name, *, record_view=True, with_quality_header=False):
        path = self._path(name)
        if path is None:
            return None
        if record_view:
            self._bump_view(name)
        content = path.read_text(encoding="utf-8")
        return self.format_quality_header(name) + content if with_quality_header else content

    @_locked
    def record_use(self, name):
        if self._path(name):
            self._bump_view(name)
            self._bump_use(name)

    def _validate(self, name, content, usage=None, *, reference_evidence=False):
        if not _NAME_PATTERN.fullmatch(name) or len(name) > _MAX_NAME_LENGTH:
            return SkillQuality(static_status="fail", hard_errors=["Invalid skill name"])
        if len(content) > _MAX_CONTENT_CHARS:
            return SkillQuality(static_status="fail", hard_errors=["Content too large"])
        existing = {n: s.path.read_text(encoding="utf-8") for n, s in self._catalog().items()}
        return evaluate_skill_content(name=name, content=content, existing=existing, usage=usage or self._get_usage(name), reference_evidence=reference_evidence)

    @staticmethod
    def _quality_result(quality):
        return {"ok": quality.static_status != "fail", "quality": quality.quality,
                "static_status": quality.static_status, "warnings": quality.warnings, "hard_errors": quality.hard_errors}

    def _check_edit(self, name, expected_version, actor):
        path = self._path(name)
        if path is None:
            state = "archived" if self._get_usage(name).state == "archived" else "not_found"
            return None, {"ok": False, "disposition": state, "error": f"Skill '{name}' {state}"}
        version = self._version(path)
        if self._get_usage(name).pinned and actor != "user":
            return None, {"ok": False, "error": "Skill is pinned", "disposition": "protected", "version": version}
        if expected_version is not None and version != expected_version:
            return None, {"ok": False, "error": "Skill changed; view the current version before retrying", "disposition": "version_conflict", "version": version}
        return path, None

    def _commit_files(self, writes, remove=()):
        guard = getattr(self, "_commit_guard", None)
        if guard:
            guard()
        if ".usage.json" in writes and self._usage_file.is_file():
            writes[".usage.previous.json"] = self._usage_file.read_bytes()
        # WAL is created only after all content has passed validation.
        encoded = {str(k): base64.b64encode(v if isinstance(v, bytes) else v.encode()).decode() for k, v in writes.items()}
        atomic_write(self._dir / ".skill-transaction.json", json.dumps({"writes": encoded, "remove": list(remove)}))
        recover_skill_transaction(self._dir)

    def _save_version(self, name, source_path, writes):
        if source_path is None:
            return
        version = self._version(source_path)
        for file in source_path.parent.rglob("*"):
            if file.is_file():
                relative = file.relative_to(source_path.parent)
                writes[str(Path(".versions") / name / version / relative)] = file.read_bytes()

    def _publish(self, name, content, quality, *, old_path=None, actor="agent", reason="", evidence=(), resources=None, removed_resources=()):
        writes = {}
        self._save_version(name, old_path, writes)
        source = self._catalog().get(name)
        if old_path and source and source.source != "memory_vault":
            # A writable override includes the built-in's complete resource tree.
            for file in old_path.parent.rglob("*"):
                if file.is_file():
                    writes[str(Path(name) / file.relative_to(old_path.parent))] = file.read_bytes()
        content = update_skill_frontmatter(content, {"quality": quality.quality, "evidence_count": quality.evidence_count})
        writes[f"{name}/SKILL.md"] = content
        writes[f"{name}/.quality.json"] = json.dumps(asdict(quality), ensure_ascii=False, indent=2)
        for relative, value in (resources or {}).items():
            writes[f"{name}/{relative}"] = value
        usage = self._usage.setdefault(name, {})
        if old_path and source and source.source != "memory_vault":
            usage.setdefault("created_by", actor)
            usage.setdefault("curator_managed", self._is_curator_managed(actor))
        now = datetime.now(timezone.utc).isoformat()
        usage["state"] = "active"
        if old_path is not None:
            usage["last_patched_at"] = now
        usage["patch_count"] = usage.get("patch_count", 0) + int(old_path is not None)
        if old_path and source and source.source != "memory_vault":
            usage.setdefault("builtin_base_version", self._version(old_path))
        usage.setdefault("revisions", []).append({"actor": actor, "reason": reason, "evidence": list(evidence),
            "previous_version": self._version(old_path) if old_path else None, "at": now})
        writes[".usage.json"] = json.dumps(self._usage, ensure_ascii=False, indent=2)
        self._commit_files(writes, remove=[f"{name}/{p}" for p in removed_resources])
        return {**self._quality_result(quality), "changed": True, "version": self.version(name), "path": str(self._dir / name / "SKILL.md")}

    @_locked
    def create(self, name, content, *, created_by="agent", reason="create", evidence=()):
        if not _NAME_PATTERN.fullmatch(name) or len(name) > _MAX_NAME_LENGTH:
            return {"ok": False, "error": "Invalid skill name"}
        if self._path(name) or (self._dir / name).exists():
            return {"ok": False, "error": "Skill already exists; view and patch it"}
        if self._get_usage(name).state == "archived":
            return {"ok": False, "error": "Skill is archived; restore its history first"}
        if not content.startswith("---"):
            return {"ok": False, "error": "SKILL.md must start with YAML frontmatter"}
        content = normalize_skill_content(content, name=name, origin=created_by, quality="draft", evidence_count=0)
        quality = self._validate(name, content)
        if quality.static_status == "fail":
            return {**self._quality_result(quality), "changed": False, "error": "Content validation failed"}
        self._usage[name] = {"created_by": created_by, "curator_managed": self._is_curator_managed(created_by),
                             "created_at": datetime.now(timezone.utc).isoformat(), "pinned": False}
        return self._publish(name, content, quality, actor=created_by, reason=reason, evidence=evidence)

    @_locked
    def patch(self, name, old_text, new_text, *, expected_version=None, actor="agent", reason="patch", evidence=()):
        path, error = self._check_edit(name, expected_version, actor)
        if error:
            return error
        content = path.read_text(encoding="utf-8")
        if not old_text or content.count(old_text) != 1:
            return {"ok": False, "changed": False, "disposition": "anchor_conflict", "version": self._version(path),
                    "matches": content.count(old_text) if old_text else 0, "error": "Patch needs one exact anchor; view current content and retry"}
        return self.edit(name, content.replace(old_text, new_text, 1), expected_version=self._version(path), actor=actor, reason=reason, evidence=evidence)

    @_locked
    def edit(self, name, new_content, *, expected_version=None, actor="agent", reason="edit", evidence=()):
        path, error = self._check_edit(name, expected_version, actor)
        if error:
            return error
        if path.read_text(encoding="utf-8") == new_content:
            return {"ok": True, "changed": False, "version": self._version(path)}
        quality = self._validate(name, new_content)
        if quality.static_status == "fail":
            return {**self._quality_result(quality), "changed": False, "error": "Content validation failed; current version preserved", "version": self._version(path)}
        return self._publish(name, new_content, quality, old_path=path, actor=actor, reason=reason, evidence=evidence)

    @_locked
    def write_reference(self, name, reference, content, *, expected_version=None, actor="agent", reason="reference", evidence=()):
        path, error = self._check_edit(name, expected_version, actor)
        if error:
            return error
        if not _NAME_PATTERN.fullmatch(reference) or reference.endswith(".md"):
            return {"ok": False, "error": "Use a reference name without path separators or .md"}
        if len(content) > _MAX_CONTENT_CHARS:
            return {"ok": False, "error": "Reference too large"}
        # Validate tool/dependency/secret content using the same checker. Detailed
        # cases belong in references, but they do not bypass content safety checks.
        body = path.read_text(encoding="utf-8")
        quality = self._validate(name, body + "\n" + content, reference_evidence=True)
        if quality.static_status == "fail":
            return {**self._quality_result(quality), "error": "Reference validation failed"}
        return self._publish(name, body, self._validate(name, body), old_path=path, actor=actor,
                             reason=reason, evidence=evidence, resources={f"references/{reference}.md": content})

    @_locked
    def archive(self, name, *, actor="agent", expected_version=None, reason="retired"):
        path, error = self._check_edit(name, expected_version, actor)
        if error:
            return error
        writes = {}
        self._save_version(name, path, writes)
        previous_archive = self._archive_dir / name / "SKILL.md"
        if previous_archive.is_file():
            self._save_version(name, previous_archive, writes)
        for file in path.parent.rglob("*"):
            if file.is_file():
                writes[str(Path(".archive") / name / file.relative_to(path.parent))] = file.read_bytes()
        self._usage.setdefault(name, {}).update(state="archived", archived_at=datetime.now(timezone.utc).isoformat(), archive_reason=reason)
        writes[".usage.json"] = json.dumps(self._usage, ensure_ascii=False, indent=2)
        remove = [name] if path.parent == self._dir / name else []
        # The archive is an exact snapshot. Keep its prior resources in .versions,
        # but do not resurrect removed resources on the next restore.
        remove += [str(file.relative_to(self._dir)) for file in previous_archive.parent.rglob("*")
                   if file.is_file() and str(file.relative_to(self._dir)) not in writes]
        self._commit_files(writes, remove)
        return {"ok": True, "changed": True, "disposition": "archived"}

    @_locked
    def restore(self, name, *, actor="agent"):
        if not _NAME_PATTERN.fullmatch(name):
            return {"ok": False, "error": "Invalid skill name"}
        path = self._archive_dir / name / "SKILL.md"
        if not path.is_file():
            return {"ok": False, "error": "No archived skill"}
        if (self._dir / name).exists():
            return {"ok": False, "error": "Active skill already exists"}
        if self._get_usage(name).pinned and actor != "user":
            return {"ok": False, "error": "Skill is pinned"}
        content = path.read_text(encoding="utf-8")
        quality = self._validate(name, content)
        if quality.static_status == "fail":
            return {**self._quality_result(quality), "error": "Archived content requires repair before restore"}
        resources = {str(f.relative_to(path.parent)): f.read_bytes() for f in path.parent.rglob("*") if f.is_file() and f.name not in {"SKILL.md", ".quality.json"}}
        return self._publish(name, content, quality, actor=actor, reason="restored", resources=resources)

    @_locked
    def versions(self, name):
        if not _NAME_PATTERN.fullmatch(name):
            return {"ok": False, "error": "Invalid skill name"}
        root = self._dir / ".versions" / name
        return {"ok": True, "changed": False, "current_version": self.version(name),
                "versions": [p.parent.name for p in sorted(root.glob("*/SKILL.md"))],
                "changes": self._usage.get(name, {}).get("revisions", [])}

    @_locked
    def restore_version(self, name, version, *, expected_version=None, actor="agent", reason="", evidence=()):
        path, error = self._check_edit(name, expected_version, actor)
        if error:
            return error
        if not re.fullmatch(r"[a-f0-9]{20}", version):
            return {"ok": False, "error": "Invalid version"}
        saved = self._dir / ".versions" / name / version / "SKILL.md"
        if not saved.is_file():
            return {"ok": False, "error": "Version not found"}
        content = saved.read_text(encoding="utf-8")
        quality = self._validate(name, content)
        if quality.static_status == "fail":
            return {**self._quality_result(quality), "error": "Saved version fails current validation"}
        resources = {str(f.relative_to(saved.parent)): f.read_bytes() for f in saved.parent.rglob("*")
                     if f.is_file() and f.name not in {"SKILL.md", ".quality.json"}}
        removed = [str(f.relative_to(path.parent)) for f in path.parent.rglob("*")
                   if f.is_file() and f.name not in {"SKILL.md", ".quality.json"} and str(f.relative_to(path.parent)) not in resources]
        return self._publish(name, content, quality, old_path=path, actor=actor,
                             reason=reason or f"restored version {version}", evidence=evidence,
                             resources=resources, removed_resources=removed)

    @_locked
    def pin(self, name, *, actor="user"):
        if actor != "user":
            return {"ok": False, "error": "Only the user can pin skills"}
        if self._path(name) is None:
            return {"ok": False, "error": "Skill not found"}
        self._usage.setdefault(name, {})["pinned"] = True
        self._save_usage()
        return {"ok": True, "changed": True}

    @_locked
    def unpin(self, name, *, actor="user"):
        if actor != "user":
            return {"ok": False, "error": "Only the user can unpin skills"}
        if name in self._usage:
            self._usage[name]["pinned"] = False
            self._save_usage()
        return {"ok": True, "changed": True}

    def agent_created_skills(self) -> list[SkillRecord]:
        """Skills eligible for curator maintenance."""
        return self.curator_managed_skills()

    def curator_managed_skills(self) -> list[SkillRecord]:
        """Skills that automated curator is allowed to mutate."""
        return [s for s in self.list_skills(include_stale=True) if s.usage.curator_managed]

    def user_owned_skills(self) -> list[SkillRecord]:
        """Skills that curator may report on but must not mutate."""
        return [s for s in self.list_skills(include_stale=True) if not s.usage.curator_managed]

    @_locked
    def mark_stale(self, name: str) -> None:
        if name in self._usage:
            self._usage[name]["state"] = "stale"
            self._save_usage()

    @_locked
    def review_quality(self, name):
        path = self._path(name)
        if path is None:
            return SkillQuality(static_status="fail", hard_errors=[f"Skill '{name}' not found"])
        quality = self._validate(name, path.read_text(encoding="utf-8"))
        # Quality is derived; never rewrite the source or package during a read.
        if path.parent == self._dir / name:
            atomic_write(path.parent / ".quality.json", json.dumps(asdict(quality), ensure_ascii=False, indent=2))
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
        except (json.JSONDecodeError, OSError) as exc:
            raise ValueError("Skill metadata unreadable; restore a verified backup before writing") from exc

    def _migrate_usage(self):
        changed = False
        for path in self._dir.glob("*/SKILL.md"):
            if path.parent.name.startswith("."):
                continue
            row = self._usage.setdefault(path.parent.name, {})
            if not row.get("created_by"):
                meta = self._parse_frontmatter(path)
                row["created_by"] = meta.get("origin") or "user"
                changed = True
            if "curator_managed" not in row:
                row["curator_managed"] = self._is_curator_managed(row["created_by"])
                changed = True
        if changed:
            self._save_usage()

    def _save_usage(self):
        if self._usage_file.is_file():
            atomic_write(self._dir / ".usage.previous.json", self._usage_file.read_text(encoding="utf-8"))
        atomic_write(self._usage_file, json.dumps(self._usage, indent=2, ensure_ascii=False))

    def _get_quality(self, name):
        path = self._path(name)
        if path is None:
            return SkillQuality(static_status="fail", hard_errors=["Skill not found"])
        return self._validate(name, path.read_text(encoding="utf-8"))

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
