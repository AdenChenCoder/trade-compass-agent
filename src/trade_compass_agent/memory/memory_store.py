"""Bounded declarative memory store — 4-tier Semantic layer (Tier 2).

Maintains two parallel states:
- Frozen snapshot: loaded once at session start, injected into system prompt.
  Never mutated mid-session (prefix-cache preservation).
- Live entries: mutated by tool calls, persisted immediately to disk.
  Tool responses reflect live state.

Entry format: JSON-lines metadata file + plain-text body.
Legacy format (§-delimited) is auto-migrated on first load.

Features (v2):
- Context fencing for safe prompt injection
- Per-entry metadata: confidence, access_count, last_accessed, source
- Ebbinghaus decay: confidence decays over time, reinforced on access
- SHA-256 dedup within 5-minute window
- Auto-archive entries below confidence threshold
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from trade_compass_agent.memory.write_gate import SemanticWriteGate

logger = logging.getLogger(__name__)

ENTRY_DELIMITER = "\n§\n"
DEFAULT_MEMORY_CHAR_LIMIT = 3000
DEFAULT_USER_CHAR_LIMIT = 1000

DECAY_LAMBDA_KNOWLEDGE = 0.05  # ~14 day half-life for KNOWLEDGE.md
DECAY_LAMBDA_USER = 0.008  # ~90 day half-life for USER.md
REINFORCED_LAMBDA_FACTOR = 0.5  # halve decay rate for frequently accessed
REINFORCE_THRESHOLD = 5  # access_count above which decay slows
ARCHIVE_CONFIDENCE_KNOWLEDGE = 0.3  # archive threshold for KNOWLEDGE entries
ARCHIVE_CONFIDENCE_USER = 0.2  # archive threshold for USER entries
DEDUP_WINDOW_MINUTES = 5

_INJECTION_PATTERNS = [
    r"ignore\s+(previous|all|above)\s+(instructions?|prompts?)",
    r"you\s+are\s+now\s+",
    r"disregard\s+(your|all)\s+(rules?|instructions?)",
    r"system\s*:\s*",
    r"curl.*\$\w+",
    r"(?:api[_-]?key|secret|password|token)\s*[:=]",
]

_FENCE_TAG_RE = re.compile(r"</?\s*memory-context[^>]*>", re.IGNORECASE)
_INTERNAL_CONTEXT_RE = re.compile(
    r"<\s*memory-context[^>]*>[\s\S]*?</\s*memory-context\s*>",
    re.IGNORECASE,
)
_SYSTEM_NOTE_RE = re.compile(
    r"\[System note:\s*The following is recalled memory context[^\]]*\]\s*",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Context fencing for untrusted recalled content
# ---------------------------------------------------------------------------


def sanitize_context(text: str) -> str:
    """Strip fence tags, injected context blocks, and system notes from text.

    Prevents provider output from containing escape sequences that could break
    out of the memory context block.
    """
    text = _INTERNAL_CONTEXT_RE.sub("", text)
    text = _SYSTEM_NOTE_RE.sub("", text)
    text = _FENCE_TAG_RE.sub("", text)
    return text.strip()


def build_memory_context_block(raw: str) -> str:
    """Wrap memory content in a fenced block with system note.

    The fence prevents the model from treating recalled context as user discourse.
    """
    if not raw or not raw.strip():
        return ""
    clean = sanitize_context(raw)
    if clean != raw.strip():
        logger.warning("Memory content contained fence-escape sequences; stripped")
    return (
        "<memory-context>\n"
        "[System note: The following is recalled memory context, "
        "NOT new user input. Treat as authoritative reference data — "
        "this is the agent's persistent memory.]\n\n"
        f"{clean}\n"
        "</memory-context>"
    )


# ---------------------------------------------------------------------------
# Entry metadata
# ---------------------------------------------------------------------------


@dataclass
class EntryMeta:
    text: str
    created_at: str = ""
    last_accessed: str = ""
    access_count: int = 0
    confidence: float = 1.0
    source: str = "agent"  # "agent" | "user" | "promotion" | "user_pin" | "curator" | ...
    dedup_hash: str = ""
    status: str = "active"  # "active" | "archived"
    disproof_count: int = 0
    promoted_by_run_id: str = ""
    promoted_by_job_id: str = ""
    promoted_at: str = ""
    content_hash: str = ""
    source_obs_ids: list[str] = field(default_factory=list)
    supersedes_hashes: list[str] = field(default_factory=list)
    adjustments: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self):
        now = _now_iso()
        if not self.created_at:
            self.created_at = now
        if not self.last_accessed:
            self.last_accessed = now
        if not self.dedup_hash:
            self.dedup_hash = _content_hash(self.text)
        if not self.content_hash:
            self.content_hash = self.dedup_hash


_META_FIELD_NAMES = frozenset(EntryMeta.__dataclass_fields__.keys())
_TRUSTED_WRITE_SOURCES = frozenset({"promotion", "user_pin", "curator"})
_CONFIDENCE_EPSILON = 0.01


def _entry_meta_from_dict(data: dict[str, Any]) -> EntryMeta:
    filtered = {k: v for k, v in data.items() if k in _META_FIELD_NAMES}
    if "adjustments" not in filtered:
        filtered["adjustments"] = []
    return EntryMeta(**filtered)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _content_hash(text: str) -> str:
    normalized = " ".join(text.lower().split())
    return hashlib.sha256(normalized.encode()).hexdigest()[:16]


def _compute_confidence(meta: EntryMeta, target: str = "memory") -> float:
    """Compute current confidence with Ebbinghaus decay (target-aware)."""
    try:
        last = datetime.fromisoformat(meta.last_accessed)
    except (ValueError, TypeError):
        return meta.confidence
    days = max(0, (datetime.now(timezone.utc) - last).total_seconds() / 86400)
    base_lambda = DECAY_LAMBDA_USER if target == "user" else DECAY_LAMBDA_KNOWLEDGE
    lam = base_lambda * REINFORCED_LAMBDA_FACTOR if meta.access_count >= REINFORCE_THRESHOLD else base_lambda
    return meta.confidence * math.exp(-lam * days)


# ---------------------------------------------------------------------------
# MemoryStore
# ---------------------------------------------------------------------------


class MemoryStore:
    """Bounded curated memory with file persistence, decay, and dedup."""

    def __init__(
        self,
        memory_dir: Path,
        memory_char_limit: int = DEFAULT_MEMORY_CHAR_LIMIT,
        user_char_limit: int = DEFAULT_USER_CHAR_LIMIT,
        write_gate: "SemanticWriteGate | None" = None,
        min_inject_confidence: float = 0.5,
    ) -> None:
        self._memory_dir = memory_dir
        self._memory_file = memory_dir / "KNOWLEDGE.md"
        self._user_file = memory_dir / "USER.md"
        self._meta_file = memory_dir / ".memory_meta.json"
        self._memory_char_limit = memory_char_limit
        self._user_char_limit = user_char_limit
        self._min_inject_confidence = min_inject_confidence
        self._recent_hashes: dict[str, datetime] = {}
        self._write_gate = write_gate

        memory_dir.mkdir(parents=True, exist_ok=True)
        self._memory_snapshot: str = ""
        self._user_snapshot: str = ""
        self._meta: dict[str, list[dict]] = {"memory": [], "user": []}
        self._load_meta()
        self._maybe_migrate_legacy()
        self._reconcile_meta()
        self.load_from_disk()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def load_from_disk(self, min_inject_confidence: float | None = None) -> None:
        """Load and freeze snapshots for this session (confidence-filtered)."""
        min_conf = min_inject_confidence if min_inject_confidence is not None else self._min_inject_confidence
        self._memory_snapshot = self._build_snapshot_text("memory", min_conf)
        self._user_snapshot = self._build_snapshot_text("user", min_conf)

    def _build_snapshot_text(self, target: str, min_confidence: float) -> str:
        active = self.list_active(target, min_confidence=min_confidence)
        if not active:
            return ""
        return ENTRY_DELIMITER.join(m.text for m in active)

    def find_by_source_obs_ids(
        self,
        obs_ids: list[str],
        target: str = "memory",
    ) -> list[EntryMeta]:
        """Find entries whose source_obs_ids overlap with the given observation ids."""
        if not obs_ids:
            return []
        wanted = set(obs_ids)
        result: list[EntryMeta] = []
        for meta in self.get_entries_with_meta(target):
            row_ids = set(meta.source_obs_ids or [])
            raw = self._meta.get(target, [])
            idx = next((i for i, m in enumerate(raw) if m.get("text") == meta.text), -1)
            if idx >= 0:
                row_ids |= set(raw[idx].get("source_obs_ids") or [])
            if wanted & row_ids:
                result.append(meta)
        return result

    @property
    def memory_entries(self) -> list[str]:
        """Live entries from disk (not frozen snapshot)."""
        return self._parse_entries(self._read_file(self._memory_file))

    @property
    def user_entries(self) -> list[str]:
        """Live user entries from disk."""
        return self._parse_entries(self._read_file(self._user_file))

    @property
    def min_inject_confidence(self) -> float:
        return self._min_inject_confidence

    def get_entries_with_meta(self, target: str = "memory") -> list[EntryMeta]:
        """Get entries with computed confidence scores."""
        metas = self._meta.get(target, [])
        entries = self.memory_entries if target == "memory" else self.user_entries
        result = []
        for i, text in enumerate(entries):
            if i < len(metas):
                meta = _entry_meta_from_dict(metas[i])
            else:
                meta = EntryMeta(text=text)
            meta.confidence = _compute_confidence(meta, target)
            result.append(meta)
        return result

    def get_active_meta(
        self,
        target: str = "memory",
        min_confidence: float = 0.0,
    ) -> list[dict[str, Any]]:
        """Return non-archived metadata dicts with confidence >= *min_confidence*."""
        rows: list[dict[str, Any]] = []
        for row in self._meta.get(target, []):
            if row.get("status", "active") == "archived":
                continue
            meta = _entry_meta_from_dict(row)
            effective = _compute_confidence(meta, target)
            if effective >= min_confidence:
                rows.append(row)
        return rows

    def list_active(
        self,
        target: str = "memory",
        min_confidence: float = 0.0,
    ) -> list[EntryMeta]:
        """Active entries with effective confidence >= *min_confidence*."""
        result: list[EntryMeta] = []
        for meta in self.get_entries_with_meta(target):
            raw = self._meta.get(target, [])
            idx = next((i for i, m in enumerate(raw) if m.get("text") == meta.text), -1)
            if idx >= 0 and raw[idx].get("status", "active") == "archived":
                continue
            if meta.confidence >= min_confidence:
                result.append(meta)
        return result

    def _is_trusted_write(self, source: str) -> bool:
        return source in _TRUSTED_WRITE_SOURCES

    @staticmethod
    def is_trusted_source(source: str) -> bool:
        return source in _TRUSTED_WRITE_SOURCES

    def _write_confidence(self, source: str, confidence: float | None) -> float:
        if confidence is None:
            if source == "user_pin":
                return 1.0
            if self.is_trusted_source(source):
                return 0.85
            confidence = 0.4
        value = float(confidence)
        if self.is_trusted_source(source):
            return max(0.0, min(1.0, value))
        cap = max(0.0, self._min_inject_confidence - _CONFIDENCE_EPSILON)
        return max(0.0, min(value, cap))

    @staticmethod
    def _merge_meta_extra(row: dict[str, Any], meta_extra: dict[str, Any] | None) -> None:
        if not meta_extra:
            return
        for key, value in meta_extra.items():
            if key in _META_FIELD_NAMES:
                row[key] = value

    def _upgrade_existing_entry(
        self,
        idx: int,
        *,
        target: str,
        source: str,
        confidence: float | None,
        meta_extra: dict[str, Any] | None,
        reason: str,
    ) -> dict[str, Any]:
        metas = self._meta.setdefault(target, [])
        while len(metas) <= idx:
            entries = self.memory_entries if target == "memory" else self.user_entries
            text = entries[len(metas)] if len(metas) < len(entries) else ""
            metas.append(asdict(EntryMeta(text=text, source="reconciled", confidence=0.85)))
        row = metas[idx]
        row["status"] = "active"
        row["source"] = source
        if confidence is not None:
            row["confidence"] = max(float(row.get("confidence", 0.0)), confidence)
        row["content_hash"] = row.get("content_hash") or row.get("dedup_hash") or _content_hash(row.get("text", ""))
        self._merge_meta_extra(row, meta_extra)
        self._save_meta()
        return {
            "ok": True,
            reason: True,
            "entry_index": idx,
            "confidence": row.get("confidence", confidence),
            "source": row.get("source", source),
        }

    def adjust_confidence(
        self,
        *,
        entry_hash: str | None = None,
        text_prefix: str | None = None,
        delta: float,
        reason: str,
        run_id: str | None = None,
        target: str = "memory",
        archive_after_disproofs: int = 2,
    ) -> dict[str, Any]:
        """Adjust stored confidence for an entry; archive after repeated disproofs."""
        metas = self._meta.get(target, [])
        idx = self._find_meta_index(metas, entry_hash=entry_hash, text_prefix=text_prefix)
        if idx is None:
            return {"ok": False, "error": "Entry not found"}

        row = metas[idx]
        previous = float(row.get("confidence", 1.0))
        new_conf = max(0.0, min(1.0, previous + delta))
        row["confidence"] = new_conf
        row["content_hash"] = row.get("content_hash") or row.get("dedup_hash") or _content_hash(row.get("text", ""))

        adjustments = list(row.get("adjustments") or [])
        adjustments.append({
            "at": _now_iso(),
            "delta": delta,
            "reason": reason,
            "run_id": run_id,
            "previous": previous,
            "new": new_conf,
        })
        row["adjustments"] = adjustments[-20:]

        if delta < 0:
            row["disproof_count"] = int(row.get("disproof_count", 0)) + 1
            if row["disproof_count"] >= archive_after_disproofs:
                row["status"] = "archived"
                row["confidence"] = 0.0
                new_conf = 0.0

        self._save_meta()
        return {
            "ok": True,
            "entry_hash": row["content_hash"],
            "previous_confidence": previous,
            "confidence": new_conf,
            "disproof_count": row.get("disproof_count", 0),
            "status": row.get("status", "active"),
        }

    @staticmethod
    def _find_meta_index(
        metas: list[dict[str, Any]],
        *,
        entry_hash: str | None,
        text_prefix: str | None,
    ) -> int | None:
        if entry_hash:
            for i, row in enumerate(metas):
                h = row.get("content_hash") or row.get("dedup_hash") or _content_hash(row.get("text", ""))
                if h == entry_hash:
                    return i
        if text_prefix:
            matches = [i for i, row in enumerate(metas) if text_prefix in row.get("text", "")]
            if len(matches) == 1:
                return matches[0]
        return None

    def archive_entry(self, text_prefix: str, target: str = "memory") -> dict[str, Any]:
        """Soft-forget: mark entry archived and zero confidence (stays on disk)."""
        metas = self._meta.get(target, [])
        idx = self._find_meta_index(metas, entry_hash=None, text_prefix=text_prefix)
        if idx is None:
            return {"ok": False, "error": f"No entry contains '{text_prefix[:50]}'"}
        row = metas[idx]
        row["confidence"] = 0.0
        row["status"] = "archived"
        self._save_meta()
        return {"ok": True, "text": (row.get("text") or "")[:80], "status": "archived"}

    def format_for_system_prompt(self) -> str:
        """Return FROZEN snapshot wrapped in context fencing."""
        parts = []
        if self._memory_snapshot:
            used = len(self._memory_snapshot)
            pct = int(used / self._memory_char_limit * 100)
            parts.append(
                f"## KNOWLEDGE ({pct}% — {used}/{self._memory_char_limit} chars)\n\n"
                f"{self._memory_snapshot}"
            )
        if self._user_snapshot:
            used = len(self._user_snapshot)
            pct = int(used / self._user_char_limit * 100)
            parts.append(
                f"## USER PROFILE ({pct}% — {used}/{self._user_char_limit} chars)\n\n"
                f"{self._user_snapshot}"
            )
        raw = "\n\n".join(parts)
        if not raw:
            return ""
        return build_memory_context_block(raw)

    def add(
        self,
        entry: str,
        target: str = "memory",
        source: str = "agent",
        confidence: float | None = None,
        meta_extra: dict[str, Any] | None = None,
        allow_supersede: bool | None = None,
        allow_reinforce: bool | None = None,
    ) -> dict[str, Any]:
        """Add a new entry with dedup check + WriteGate. Returns status dict."""
        from trade_compass_agent.memory.write_gate import jaccard_similarity, JACCARD_THRESHOLD

        entry = entry.strip()
        if not entry:
            return {"ok": False, "error": "Empty entry"}
        if self._scan_threats(entry):
            return {"ok": False, "error": "Content blocked by safety filter"}

        h = _content_hash(entry)
        write_confidence = self._write_confidence(source, confidence)
        trusted_write = self._is_trusted_write(source)
        if self._is_recent_dup(h) and not trusted_write:
            return {"ok": False, "error": "Duplicate within dedup window"}

        file_path = self._memory_file if target == "memory" else self._user_file
        char_limit = self._memory_char_limit if target == "memory" else self._user_char_limit

        entries = self._parse_entries(self._read_file(file_path))
        metas = self._meta.get(target, [])
        can_supersede = trusted_write if allow_supersede is None else allow_supersede
        can_reinforce = trusted_write if allow_reinforce is None else allow_reinforce

        # Check exact dup in existing entries. Trusted writes can revive/upgrade
        # low-confidence or archived rows; low-trust writes cannot boost existing memory.
        for i, existing in enumerate(entries):
            if _content_hash(existing) != h:
                continue
            if trusted_write:
                return self._upgrade_existing_entry(
                    i,
                    target=target,
                    source=source,
                    confidence=write_confidence,
                    meta_extra=meta_extra,
                    reason="duplicate",
                )
            return {"ok": False, "error": "Duplicate entry"}

        # Similar entry → supersede or reinforce.
        for i, existing in enumerate(entries):
            sim = jaccard_similarity(entry, existing)
            if sim > JACCARD_THRESHOLD:
                self._record_hash(h)
                row = metas[i] if i < len(metas) else {}
                existing_conf = float(row.get("confidence", 1.0))
                existing_archived = row.get("status", "active") == "archived"
                existing_injects = (not existing_archived) and existing_conf >= self._min_inject_confidence

                if trusted_write and (existing_archived or not existing_injects):
                    result = self.replace(
                        existing[:50],
                        entry,
                        target,
                        source=source,
                        confidence=write_confidence,
                        meta_extra=meta_extra,
                    )
                    if result.get("ok"):
                        return {"ok": True, "upgraded": True, "superseded": existing[:40]}

                if can_supersede and len(entry) > len(existing) * 1.2:
                    result = self.replace(
                        existing[:50],
                        entry,
                        target,
                        source=source,
                        confidence=write_confidence,
                        meta_extra=meta_extra,
                    )
                    if result.get("ok"):
                        return {"ok": True, "superseded": existing[:40]}

                if can_reinforce and existing_injects:
                    self.reinforce(existing[:50], target)
                return {
                    "ok": False,
                    "merged": True,
                    "error": (
                        f"Similar entry reinforced: '{existing[:40]}…'"
                        if can_reinforce and existing_injects
                        else f"Similar entry exists: '{existing[:40]}…'"
                    ),
                }

        # WriteGate dedup check (quality checks moved to promotion stage)
        if self._write_gate:
            gate_entries = [
                e for i, e in enumerate(entries)
                if i >= len(metas)
                or (
                    metas[i].get("status", "active") != "archived"
                    and float(metas[i].get("confidence", 1.0)) >= self._min_inject_confidence
                )
            ]
            admitted, reason = self._write_gate.should_admit(entry, target, gate_entries)
            if not admitted:
                return {"ok": False, "error": f"WriteGate rejected: {reason}"}

        new_content = ENTRY_DELIMITER.join(entries + [entry])
        if len(new_content) > char_limit:
            return {
                "ok": False,
                "error": f"Would exceed {target} limit ({len(new_content)}/{char_limit} chars). Remove old entries first.",
            }

        self._atomic_write(file_path, new_content)
        self._record_hash(h)

        meta = EntryMeta(text=entry, source=source)
        meta.confidence = write_confidence
        meta_dict = asdict(meta)
        if meta_extra:
            for key, value in meta_extra.items():
                if key in _META_FIELD_NAMES:
                    meta_dict[key] = value
        self._meta.setdefault(target, []).append(meta_dict)
        self._save_meta()

        return {
            "ok": True,
            "entries": len(entries) + 1,
            "chars_used": len(new_content),
            "limit": char_limit,
            "confidence": meta_dict.get("confidence", meta.confidence),
            "source": source,
        }

    def replace(
        self,
        old_text: str,
        new_text: str,
        target: str = "memory",
        *,
        source: str | None = None,
        confidence: float | None = None,
        meta_extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Replace text within an entry (supersession: marks old as superseded)."""
        new_text = new_text.strip()
        if self._scan_threats(new_text):
            return {"ok": False, "error": "Content blocked by safety filter"}

        file_path = self._memory_file if target == "memory" else self._user_file
        entries = self._parse_entries(self._read_file(file_path))

        matches = [i for i, e in enumerate(entries) if old_text in e]
        if len(matches) == 0:
            return {"ok": False, "error": f"No entry contains '{old_text[:50]}'"}
        if len(matches) > 1:
            return {"ok": False, "error": f"Ambiguous: {len(matches)} entries match. Be more specific."}

        idx = matches[0]
        old_entry = entries[idx]
        entries[idx] = new_text
        new_content = ENTRY_DELIMITER.join(entries)

        char_limit = self._memory_char_limit if target == "memory" else self._user_char_limit
        if len(new_content) > char_limit:
            return {"ok": False, "error": f"Would exceed {target} limit ({len(new_content)}/{char_limit} chars)."}

        self._atomic_write(file_path, new_content)

        # Supersession chain: record what was replaced
        metas = self._meta.get(target, [])
        old_hash = _content_hash(old_entry)
        new_hash = _content_hash(new_text)
        if idx < len(metas):
            metas[idx]["text"] = new_text
            metas[idx]["dedup_hash"] = new_hash
            metas[idx]["content_hash"] = new_hash
            metas[idx]["last_accessed"] = _now_iso()
            metas[idx]["access_count"] = metas[idx].get("access_count", 0) + 1
            metas[idx]["supersedes"] = old_hash
            supersedes_hashes = list(metas[idx].get("supersedes_hashes") or [])
            if old_hash not in supersedes_hashes:
                supersedes_hashes.append(old_hash)
            metas[idx]["supersedes_hashes"] = supersedes_hashes[-20:]
            metas[idx]["status"] = "active"
            if source is not None:
                metas[idx]["source"] = source
            if confidence is not None:
                metas[idx]["confidence"] = self._write_confidence(
                    str(metas[idx].get("source", "")),
                    confidence,
                )
            self._merge_meta_extra(metas[idx], meta_extra)

        # Archive the superseded entry
        superseded = self._meta.setdefault("_superseded", [])
        superseded.append({
            "text": old_entry, "hash": old_hash,
            "superseded_by": new_hash, "superseded_at": _now_iso(),
        })
        if len(superseded) > 50:
            self._meta["_superseded"] = superseded[-50:]
        self._save_meta()

        return {"ok": True, "entry_index": idx, "superseded": old_hash[:8]}

    def remove(self, text: str, target: str = "memory") -> dict[str, Any]:
        """Remove an entry containing the given text."""
        file_path = self._memory_file if target == "memory" else self._user_file
        entries = self._parse_entries(self._read_file(file_path))

        matches = [i for i, e in enumerate(entries) if text in e]
        if len(matches) == 0:
            return {"ok": False, "error": f"No entry contains '{text[:50]}'"}
        if len(matches) > 1:
            return {"ok": False, "error": f"Ambiguous: {len(matches)} entries match."}

        idx = matches[0]
        entries.pop(idx)
        new_content = ENTRY_DELIMITER.join(entries)
        self._atomic_write(file_path, new_content)

        metas = self._meta.get(target, [])
        if idx < len(metas):
            metas.pop(idx)
        self._save_meta()

        return {"ok": True, "remaining": len(entries)}

    def reinforce(self, text: str, target: str = "memory") -> dict[str, Any]:
        """Reinforce a memory entry (called by background review on access).

        Low-trust sources can accumulate usage signal, but cannot cross the
        injection confidence threshold without promotion/user/curator review.
        """
        entries = self.memory_entries if target == "memory" else self.user_entries
        matches = [i for i, e in enumerate(entries) if text in e]
        if not matches:
            return {"ok": False, "error": "Entry not found"}

        idx = matches[0]
        metas = self._meta.get(target, [])
        if idx < len(metas):
            if metas[idx].get("status", "active") == "archived":
                return {"ok": False, "error": "Entry is archived"}
            metas[idx]["access_count"] = metas[idx].get("access_count", 0) + 1
            metas[idx]["last_accessed"] = _now_iso()
            current = float(metas[idx].get("confidence", 1.0))
            next_confidence = min(1.0, current + 0.1)
            if not self.is_trusted_source(str(metas[idx].get("source", ""))):
                cap = max(0.0, self._min_inject_confidence - _CONFIDENCE_EPSILON)
                next_confidence = min(next_confidence, cap)
            metas[idx]["confidence"] = max(current, next_confidence)
            self._save_meta()
            return {"ok": True, "confidence": metas[idx]["confidence"]}
        return {"ok": True}

    def archive_stale(self, target: str = "memory") -> list[str]:
        """Archive entries with confidence below threshold. Returns archived texts."""
        threshold = ARCHIVE_CONFIDENCE_USER if target == "user" else ARCHIVE_CONFIDENCE_KNOWLEDGE
        entries_meta = self.get_entries_with_meta(target)
        to_archive = [m for m in entries_meta if m.status != "archived" and m.confidence < threshold]
        archived = []
        for m in to_archive:
            result = self.archive_entry(m.text[:50], target)
            if result.get("ok"):
                archived.append(m.text)
                logger.info("Archived stale memory (confidence=%.2f): %s", m.confidence, m.text[:40])
        return archived

    def archive_inactive(self, target: str = "memory", stale_days: int = 90) -> list[str]:
        """Soft-archive entries not accessed within *stale_days*. Skips user_pin."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=stale_days)
        archived: list[str] = []
        for row in self._meta.get(target, []):
            if row.get("status", "active") == "archived":
                continue
            if row.get("source") == "user_pin":
                continue
            last_str = row.get("last_accessed") or row.get("created_at") or ""
            try:
                last_dt = datetime.fromisoformat(last_str)
                if last_dt.tzinfo is None:
                    last_dt = last_dt.replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                continue
            if last_dt >= cutoff:
                continue
            text = row.get("text") or ""
            if not text:
                continue
            result = self.archive_entry(text[:50], target)
            if result.get("ok"):
                archived.append(text[:80])
                logger.info("Archived inactive memory (%d days): %s", stale_days, text[:40])
        return archived

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    # _reinforce_all_on_inject removed: blanket access bumping on every session
    # nullified Ebbinghaus decay. Entries are now only reinforced when explicitly
    # accessed via reinforce() or search_memory recall.

    def _is_recent_dup(self, h: str) -> bool:
        now = datetime.now(timezone.utc)
        # Cleanup old entries
        self._recent_hashes = {
            k: v for k, v in self._recent_hashes.items()
            if (now - v) < timedelta(minutes=DEDUP_WINDOW_MINUTES)
        }
        return h in self._recent_hashes

    def _record_hash(self, h: str) -> None:
        self._recent_hashes[h] = datetime.now(timezone.utc)

    def _read_file(self, path: Path) -> str:
        if not path.is_file():
            return ""
        return path.read_text(encoding="utf-8").strip()

    def _parse_entries(self, content: str) -> list[str]:
        if not content:
            return []
        return [e.strip() for e in content.split("§") if e.strip()]

    def _atomic_write(self, path: Path, content: str) -> None:
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

    def _scan_threats(self, text: str) -> bool:
        lower = text.lower()
        for pattern in _INJECTION_PATTERNS:
            if re.search(pattern, lower):
                logger.warning("Memory injection attempt blocked: %s", text[:60])
                return True
        return False

    def _load_meta(self) -> None:
        if self._meta_file.is_file():
            try:
                self._meta = json.loads(self._meta_file.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                self._meta = {"memory": [], "user": []}

    def _save_meta(self) -> None:
        self._atomic_write(
            self._meta_file,
            json.dumps(self._meta, indent=2, ensure_ascii=False),
        )

    def _reconcile_meta(self) -> None:
        """Align .memory_meta.json with actual file entries to fix drift."""
        changed = False
        for target, file_path in [("memory", self._memory_file), ("user", self._user_file)]:
            entries = self._parse_entries(self._read_file(file_path))
            metas = self._meta.get(target, [])
            if len(metas) == len(entries):
                continue
            if not entries:
                if metas:
                    self._meta[target] = []
                    changed = True
                continue
            entry_hashes = [_content_hash(e) for e in entries]
            meta_by_hash = {m.get("dedup_hash", ""): m for m in metas}
            new_metas = []
            for i, e in enumerate(entries):
                h = entry_hashes[i]
                if h in meta_by_hash:
                    new_metas.append(meta_by_hash[h])
                else:
                    new_metas.append(asdict(EntryMeta(text=e, source="reconciled", confidence=0.85)))
            self._meta[target] = new_metas
            changed = True
            logger.info(
                "Reconciled %s meta: %d entries in file, was %d in meta",
                target, len(entries), len(metas),
            )
        if changed:
            self._save_meta()

    def _maybe_migrate_legacy(self) -> None:
        """Migrate legacy §-delimited entries to have metadata if none exists."""
        if self._meta.get("memory") or self._meta.get("user"):
            return
        for target, file_path in [("memory", self._memory_file), ("user", self._user_file)]:
            entries = self._parse_entries(self._read_file(file_path))
            if entries and not self._meta.get(target):
                self._meta[target] = [
                    asdict(EntryMeta(text=e, source="legacy", confidence=0.85))
                    for e in entries
                ]
        if self._meta.get("memory") or self._meta.get("user"):
            self._save_meta()
            logger.info("Migrated legacy memory entries to metadata format")
