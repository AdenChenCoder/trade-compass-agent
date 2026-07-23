from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ENTRY_DELIMITER = "\n§\n"
DEFAULT_RULES_CHAR_LIMIT = 4000
HUMAN_ACTORS = {"user", "web", "cli"}


@dataclass(frozen=True)
class RuleEntry:
    id: str
    text: str
    enabled: bool = True
    updated_by: str = "user"
    created_at: str = ""
    updated_at: str = ""
    content_hash: str = ""


class RulesStore:
    """Human-owned top-level rules stored in memory_vault/RULES.md."""

    def __init__(self, memory_dir: Path, *, char_limit: int = DEFAULT_RULES_CHAR_LIMIT) -> None:
        self._dir = memory_dir
        self._rules_file = memory_dir / "RULES.md"
        self._meta_file = memory_dir / ".rules_meta.json"
        self._char_limit = char_limit
        self._dir.mkdir(parents=True, exist_ok=True)

    def read_for_prompt(self) -> str:
        entries = self._parse_entries(self._read_rules())
        meta_by_hash: dict[str, list[dict[str, Any]]] = {}
        for item in self._load_meta():
            meta_by_hash.setdefault(str(item.get("content_hash", "")), []).append(item)

        enabled_entries: list[str] = []
        for text in entries:
            h = _content_hash(text)
            item = meta_by_hash.get(h, [{}]).pop(0)
            if bool(item.get("enabled", True)):
                enabled_entries.append(text)
        return ENTRY_DELIMITER.join(enabled_entries).strip()

    def list_entries(self) -> list[RuleEntry]:
        entries = self._parse_entries(self._read_rules())
        meta = self._reconcile_meta(entries)
        return [
            RuleEntry(
                id=str(item.get("id", _entry_id(text))),
                text=text,
                enabled=bool(item.get("enabled", True)),
                updated_by=str(item.get("updated_by", "user")),
                created_at=str(item.get("created_at", "")),
                updated_at=str(item.get("updated_at", "")),
                content_hash=str(item.get("content_hash", _content_hash(text))),
            )
            for text, item in zip(entries, meta, strict=False)
        ]

    def chars_used(self) -> int:
        return len(self._read_rules())

    @property
    def char_limit(self) -> int:
        return self._char_limit

    def version(self) -> str:
        content = self._read_rules()
        mtime = self._rules_file.stat().st_mtime_ns if self._rules_file.exists() else 0
        return f"{mtime:x}-{_content_hash(content)}"

    def add(self, text: str, *, actor: str) -> dict[str, Any]:
        self._require_human(actor)
        text = text.strip()
        if not text:
            return {"ok": False, "error": "Empty rule"}
        entries = self._parse_entries(self._read_rules())
        entries.append(text)
        return self._replace_entries(entries, actor=actor)

    def replace(self, entry_id: str, text: str, *, actor: str) -> dict[str, Any]:
        self._require_human(actor)
        text = text.strip()
        if not text:
            return {"ok": False, "error": "Empty rule"}
        entries = self.list_entries()
        for idx, entry in enumerate(entries):
            if entry.id == entry_id:
                raw = [item.text for item in entries]
                raw[idx] = text
                return self._replace_entries(raw, actor=actor, preserve_ids={idx: entry.id})
        return {"ok": False, "error": f"Rule not found: {entry_id}"}

    def remove(self, entry_id: str, *, actor: str) -> dict[str, Any]:
        self._require_human(actor)
        entries = self.list_entries()
        kept = [entry.text for entry in entries if entry.id != entry_id]
        if len(kept) == len(entries):
            return {"ok": False, "error": f"Rule not found: {entry_id}"}
        return self._replace_entries(kept, actor=actor)

    def replace_all(self, content: str, *, actor: str) -> dict[str, Any]:
        self._require_human(actor)
        entries = self._parse_entries(content)
        return self._replace_entries(entries, actor=actor)

    def _replace_entries(
        self,
        entries: list[str],
        *,
        actor: str,
        preserve_ids: dict[int, str] | None = None,
    ) -> dict[str, Any]:
        content = ENTRY_DELIMITER.join(entry.strip() for entry in entries if entry.strip()).strip()
        if len(content) > self._char_limit:
            return {
                "ok": False,
                "error": f"Would exceed rules limit ({len(content)}/{self._char_limit} chars).",
            }
        self._atomic_write(self._rules_file, content)
        self._save_meta_for_entries(self._parse_entries(content), actor=actor, preserve_ids=preserve_ids)
        return {
            "ok": True,
            "entries": len(self._parse_entries(content)),
            "chars_used": len(content),
            "limit": self._char_limit,
            "version": self.version(),
        }

    def _reconcile_meta(self, entries: list[str]) -> list[dict[str, Any]]:
        existing = self._load_meta()
        by_hash: dict[str, list[dict[str, Any]]] = {}
        for item in existing:
            by_hash.setdefault(str(item.get("content_hash", "")), []).append(item)

        changed = len(existing) != len(entries)
        reconciled: list[dict[str, Any]] = []
        for text in entries:
            h = _content_hash(text)
            if by_hash.get(h):
                item = dict(by_hash[h].pop(0))
                item["content_hash"] = h
                item.setdefault("id", _entry_id(text))
                item.setdefault("enabled", True)
                item.setdefault("updated_by", "user")
                item.setdefault("created_at", "")
                item.setdefault("updated_at", "")
            else:
                item = _meta_item(text, actor="user", created_at="")
                changed = True
            reconciled.append(item)

        if changed or reconciled != existing:
            self._save_meta(reconciled)
        return reconciled

    def _save_meta_for_entries(
        self,
        entries: list[str],
        *,
        actor: str,
        preserve_ids: dict[int, str] | None = None,
    ) -> None:
        preserve_ids = preserve_ids or {}
        old = self._load_meta()
        old_by_hash: dict[str, list[dict[str, Any]]] = {}
        old_by_id: dict[str, dict[str, Any]] = {}
        for item in old:
            old_by_hash.setdefault(str(item.get("content_hash", "")), []).append(item)
            old_by_id[str(item.get("id", ""))] = item

        meta: list[dict[str, Any]] = []
        now = _now()
        for idx, text in enumerate(entries):
            h = _content_hash(text)
            if idx in preserve_ids:
                preserved = old_by_id.get(preserve_ids[idx], {})
                item = _meta_item(
                    text,
                    actor=actor,
                    entry_id=preserve_ids[idx],
                    created_at=str(preserved.get("created_at", "")),
                    updated_at=now,
                )
            elif old_by_hash.get(h):
                item = dict(old_by_hash[h].pop(0))
                item.update({"content_hash": h, "updated_by": actor, "updated_at": now})
            else:
                item = _meta_item(text, actor=actor, updated_at=now)
            meta.append(item)
        self._save_meta(meta)

    def _load_meta(self) -> list[dict[str, Any]]:
        if not self._meta_file.is_file():
            return []
        try:
            raw = json.loads(self._meta_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return []
        if isinstance(raw, list):
            return [item for item in raw if isinstance(item, dict)]
        if isinstance(raw, dict) and isinstance(raw.get("entries"), list):
            return [item for item in raw["entries"] if isinstance(item, dict)]
        return []

    def _save_meta(self, entries: list[dict[str, Any]]) -> None:
        self._atomic_write(
            self._meta_file,
            json.dumps({"entries": entries}, ensure_ascii=False, indent=2),
        )

    def _read_rules(self) -> str:
        if not self._rules_file.is_file():
            return ""
        return self._rules_file.read_text(encoding="utf-8").strip()

    def _parse_entries(self, content: str) -> list[str]:
        if not content.strip():
            return []
        return [entry.strip() for entry in content.split("§") if entry.strip()]

    def _require_human(self, actor: str) -> None:
        if actor not in HUMAN_ACTORS:
            raise PermissionError("RULES.md is human-owned and cannot be modified by agents")

    def _atomic_write(self, path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        try:
            os.write(fd, content.encode("utf-8"))
            os.close(fd)
            os.replace(tmp, path)
        except Exception:
            try:
                os.close(fd)
            except OSError:
                pass
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise


def _meta_item(
    text: str,
    *,
    actor: str,
    entry_id: str | None = None,
    created_at: str | None = None,
    updated_at: str | None = None,
) -> dict[str, Any]:
    now = _now()
    return asdict(
        RuleEntry(
            id=entry_id or _entry_id(text),
            text=text,
            enabled=True,
            updated_by=actor,
            created_at=now if created_at is None else created_at,
            updated_at=updated_at or now,
            content_hash=_content_hash(text),
        )
    ) | {"text": text}


def _entry_id(text: str) -> str:
    return "rule_" + _content_hash(text)[:12]


def _content_hash(text: str) -> str:
    normalized = "\n".join(line.rstrip() for line in text.strip().splitlines())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
