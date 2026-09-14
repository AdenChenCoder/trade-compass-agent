"""Bounded declarative memory store — 4-tier Semantic layer (Tier 2).

Maintains two parallel states:
- Frozen snapshot: loaded once at session start, injected into system prompt.
  Never mutated mid-session (prefix-cache preservation).
- Live entries: mutated by tool calls, persisted immediately to disk.
  Tool responses reflect live state.

The JSON ledger commits text, identity, lifecycle and provenance together.
KNOWLEDGE.md / USER.md are effective-only projections. Candidates and retired
versions remain accessible in the ledger and the normal memory API.
Age/usage informs review; neither changes admission by itself.
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
from contextlib import contextmanager
from copy import deepcopy
from functools import wraps
from uuid import uuid4
import threading

from trade_compass_agent.concurrency import file_transaction
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
    entry_id: str = field(default_factory=lambda: uuid4().hex)
    version: int = 1
    reason: str = ""
    evidence: list[str] = field(default_factory=list)
    needs_review: bool = False
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


def _live(method):
    """Refresh under the shared transaction before every live read or mutation."""
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        with self._transaction():
            return method(self, *args, **kwargs)
    return wrapped


class MemoryStore:
    """Versioned memory ledger with a bounded, explicit effective core.

    .memory_meta.json is the authoritative record (including candidates/history).
    Markdown files are recoverable projections of the effective core only. A
    single atomic ledger replacement commits text, identity, provenance and state.
    All consumers use this boundary; session prompt snapshots remain frozen.
    """

    def __init__(self, memory_dir: Path,
                 memory_char_limit: int = DEFAULT_MEMORY_CHAR_LIMIT,
                 user_char_limit: int = DEFAULT_USER_CHAR_LIMIT,
                 write_gate: "SemanticWriteGate | None" = None,
                 min_inject_confidence: float = 0.5) -> None:
        self._memory_dir = Path(memory_dir)
        self._memory_file = self._memory_dir / "KNOWLEDGE.md"
        self._user_file = self._memory_dir / "USER.md"
        self._meta_file = self._memory_dir / ".memory_meta.json"
        self._previous_file = self._memory_dir / ".memory_meta.previous.json"
        self._lock_file = self._memory_dir / ".memory.lock"
        self._memory_char_limit = memory_char_limit
        self._user_char_limit = user_char_limit
        self._min_inject_confidence = min_inject_confidence
        self._write_gate = write_gate
        self._memory_snapshot = self._user_snapshot = ""
        self._meta: dict[str, Any] = {}
        self._local = threading.local()
        self._memory_dir.mkdir(parents=True, exist_ok=True)
        self.load_from_disk()

    @contextmanager
    def _transaction(self):
        with file_transaction(self._lock_file):
            outer = not getattr(self._local, "depth", 0)
            self._local.depth = getattr(self._local, "depth", 0) + 1
            try:
                if outer:
                    self._load_meta()
                yield
            finally:
                self._local.depth -= 1

    def _limit(self, target):
        if target not in ("memory", "user"):
            raise ValueError("target must be memory or user")
        return self._memory_char_limit if target == "memory" else self._user_char_limit

    def _core(self, target):
        self._limit(target)
        return [r for r in self._meta.get(target, []) if r.get("status") == "active"]

    def _chars(self, target):
        return len(ENTRY_DELIMITER.join(r["text"] for r in self._core(target)))

    def _fits(self, target, rows):
        return len(ENTRY_DELIMITER.join(r["text"] for r in rows if r["status"] == "active")) <= self._limit(target)

    @_live
    def capacity(self, target="memory") -> dict[str, Any]:
        rows = self._meta.get(target, [])
        used, limit = self._chars(target), self._limit(target)
        return {"chars_used": used, "limit": limit, "revision": self._meta["revision"],
                "active_count": sum(r["status"] == "active" for r in rows),
                "candidate_count": sum(r["status"] == "candidate" for r in rows),
                "archived_count": sum(r["status"] == "archived" for r in rows),
                "pressure": used >= int(limit * .9),
                "maintenance_needed": used >= int(limit * .9) or any(
                    r["status"] == "candidate" or r.get("needs_review") for r in rows)}

    @_live
    def review_fingerprint(self):
        values = [(r["entry_id"], r["version"], r["status"], r.get("needs_review"))
                  for target in ("memory", "user") for r in self._meta[target]]
        return hashlib.sha256(json.dumps(values).encode()).hexdigest()

    @_live
    def review_due(self):
        return self.capacity()["maintenance_needed"] and self._meta.get("background_reviewed_fingerprint") != self.review_fingerprint()

    @_live
    def snapshot(self, target="memory", *, include_history=True):
        return {"entries": self.get_entries_with_meta(target, include_history=include_history), **self.capacity(target)}

    @property
    def min_inject_confidence(self):
        return self._min_inject_confidence

    @property
    @_live
    def revision(self):
        return self._meta["revision"]

    @property
    @_live
    def memory_entries(self):
        """Compatibility accessor: current records, including inactive records."""
        return [r["text"] for r in self._meta["memory"]]

    @property
    @_live
    def user_entries(self):
        return [r["text"] for r in self._meta["user"]]

    @_live
    def get_entries_with_meta(self, target="memory", *, include_history=False):
        self._limit(target)
        rows = list(self._meta[target])
        if include_history:
            rows += [r for r in self._meta.get("history", []) if r.get("target") == target]
        return [_entry_meta_from_dict(deepcopy(r)) for r in rows]

    @_live
    def get_active_meta(self, target="memory", min_confidence=0.0):
        # Admission is explicit. Age triggers re-evaluation, never silent eviction.
        return deepcopy([r for r in self._core(target) if r["confidence"] >= min_confidence])

    @_live
    def list_active(self, target="memory", min_confidence=0.0):
        return [_entry_meta_from_dict(r) for r in self.get_active_meta(target, min_confidence)]

    @_live
    def find_by_source_obs_ids(self, obs_ids, target="memory"):
        wanted = set(obs_ids)
        return [m for m in self.get_entries_with_meta(target) if wanted.intersection(m.source_obs_ids)]

    @_live
    def load_from_disk(self, min_inject_confidence=None):
        # State and quota use the same set; confidence filtering occurs at admission.
        self._memory_snapshot = ENTRY_DELIMITER.join(r["text"] for r in self._core("memory"))
        self._user_snapshot = ENTRY_DELIMITER.join(r["text"] for r in self._core("user"))

    def format_for_system_prompt(self):
        parts = []
        for label, snapshot, limit in (("KNOWLEDGE", self._memory_snapshot, self._memory_char_limit),
                                      ("USER PROFILE", self._user_snapshot, self._user_char_limit)):
            if snapshot:
                used = len(snapshot)
                parts.append(f"## {label} ({int(used / limit * 100)}% — {used}/{limit} chars)\n\n{snapshot}")
        return build_memory_context_block("\n\n".join(parts)) if parts else ""

    @staticmethod
    def is_trusted_source(source):
        return source in _TRUSTED_WRITE_SOURCES

    def _write_confidence(self, source, confidence):
        if source == "user_pin":
            return 1.0
        value = float(confidence if confidence is not None else (.85 if self.is_trusted_source(source) else .4))
        cap = 1.0 if self.is_trusted_source(source) else max(0, self._min_inject_confidence - .01)
        return max(0.0, min(cap, value))

    def _new_row(self, text, source, confidence=None, meta_extra=None):
        conf = self._write_confidence(source, confidence)
        row = asdict(EntryMeta(text=text, source=source, confidence=conf,
                              status="active" if conf >= self._min_inject_confidence else "candidate"))
        # Callers can attach evidence, never replace identity or lifecycle fields.
        for key in ("source_obs_ids", "promoted_by_run_id", "promoted_by_job_id", "promoted_at",
                    "supersedes_hashes", "reason", "evidence"):
            if meta_extra and key in meta_extra:
                row[key] = deepcopy(meta_extra[key])
        if not row["reason"]:
            row["reason"] = "admitted" if row["status"] == "active" else "awaiting_evidence"
        return row

    def _receipt(self, row, target, *, changed=True, **extra):
        return {"ok": True, "changed": changed, "accepted": row["status"] == "active",
                "disposition": {"active": "adopted", "candidate": "pending", "archived": "retired"}[row["status"]],
                "entry_id": row["entry_id"], "version": row["version"], "status": row["status"],
                "confidence": row["confidence"], "source": row["source"], "reason": row["reason"],
                **self.capacity(target), **extra}

    def _error(self, message, disposition="failed", **extra):
        return {"ok": False, "changed": False, "disposition": disposition, "error": message, **extra}

    def _validate_text(self, text):
        if not text.strip():
            return "Empty entry"
        if "§" in text:
            return "Entry delimiter is not allowed inside a memory entry"
        if self._scan_threats(text):
            return "Content blocked by safety filter"
        return None

    @_live
    def add(self, entry, target="memory", source="agent", confidence=None, meta_extra=None,
            allow_supersede=None, allow_reinforce=None):
        entry = entry.strip()
        error = self._validate_text(entry)
        if error:
            return self._error(error)
        self._limit(target)
        new = self._new_row(entry, source, confidence, meta_extra)
        for row in sorted(self._meta[target], key=lambda r: (r["source"] != "user_pin", r["status"] != "active", r["status"] == "archived")):
            if row["content_hash"] != new["content_hash"]:
                continue
            if not self.is_trusted_source(source):
                return self._receipt(row, target, changed=False, duplicate=True)
            if row["source"] == "user_pin" and source != "user_pin":
                return self._receipt(row, target, changed=False, duplicate=True)
            new["entry_id"], new["version"] = row["entry_id"], row["version"] + 1
            new["source_obs_ids"] = sorted(set(row.get("source_obs_ids", []) + new["source_obs_ids"]))
            proposed = [new if r is row else r for r in self._meta[target]]
            if not self._fits(target, proposed):
                if source == "user_pin" or row["status"] == "active":
                    return self._error("Core capacity unavailable; existing record preserved", "capacity_blocked", **self.capacity(target))
                new.update(status="candidate", reason="capacity_review_required")
            if all(new[k] == row.get(k) for k in ("text", "source", "confidence", "status", "source_obs_ids")):
                return self._receipt(row, target, changed=False, duplicate=True)
            self._remember(row, target, "admission_updated", new["entry_id"])
            self._meta[target] = proposed
            self._save_meta()
            return self._receipt(new, target, duplicate=True)
        # Similarity is a review hint, never proof that longer wording is better.
        if self._write_gate and source != "user_pin":
            admitted, reason = self._write_gate.should_admit(entry, target, [r["text"] for r in self._core(target)])
            if not admitted:
                new["status"], new["reason"] = "candidate", f"review_similarity: {reason}"
        if not self._fits(target, self._meta[target] + [new]):
            if source == "user_pin":
                return self._error("Pinned content would exceed core capacity", "capacity_blocked", **self.capacity(target))
            new["status"], new["reason"] = "candidate", "capacity_review_required"
        self._meta[target].append(new)
        self._save_meta()
        return self._receipt(new, target, entries=len(self._meta[target]))

    def _locate(self, target, text="", entry_id=None, expected_version=None):
        self._limit(target)
        matches = [r for r in self._meta[target]
                   if (r["entry_id"] == entry_id if entry_id else bool(text) and text in r["text"])]
        if len(matches) != 1:
            return None, self._error("Entry not found" if not matches else "Ambiguous entry; use entry_id")
        row = matches[0]
        if expected_version is not None and row["version"] != expected_version:
            return None, self._error("Entry changed; read the current version before retrying", "version_conflict",
                                     entry_id=row["entry_id"], version=row["version"])
        return row, None

    def _remember(self, row, target, reason, successor=""):
        old = deepcopy(row)
        old.update(target=target, status="archived", reason=reason, retired_at=_now_iso(), successor_id=successor)
        self._meta.setdefault("history", []).append(old)

    @_live
    def replace(self, old_text, new_text, target="memory", *, source=None, confidence=None,
                meta_extra=None, entry_id=None, expected_version=None, actor="curator", reason="revision"):
        error = self._validate_text(new_text)
        if error:
            return self._error(error)
        row, error = self._locate(target, old_text, entry_id, expected_version)
        if error:
            return error
        if row["source"] == "user_pin" and actor != "user":
            return self._error("Pinned memory can only be changed by the user", "protected")
        new = self._new_row(new_text.strip(), source or row["source"],
                            confidence if confidence is not None else row["confidence"], meta_extra)
        new.update(entry_id=row["entry_id"], version=row["version"] + 1,
                   supersedes_hashes=list(dict.fromkeys(row.get("supersedes_hashes", []) + [row["content_hash"]])),
                   reason=reason)
        proposed = [new if r is row else r for r in self._meta[target]]
        if not self._fits(target, proposed):
            return self._error("Replacement would exceed core capacity; original preserved", "capacity_blocked", **self.capacity(target))
        self._remember(row, target, reason, new["entry_id"])
        self._meta[target] = proposed
        self._save_meta()
        return self._receipt(new, target, superseded=row["content_hash"])

    @_live
    def archive_entry(self, text_prefix="", target="memory", *, entry_id=None, expected_version=None,
                      actor="curator", reason="retired", evidence=None):
        row, error = self._locate(target, text_prefix, entry_id, expected_version)
        if error:
            return error
        if row["source"] == "user_pin" and actor != "user":
            return self._error("Pinned memory can only be changed by the user", "protected")
        if row["status"] == "archived":
            return self._receipt(row, target, changed=False)
        self._remember(row, target, reason)
        row.update(status="archived", reason=reason, version=row["version"] + 1)
        row["evidence"] = list(dict.fromkeys(row.get("evidence", []) + list(evidence or [])))
        self._save_meta()
        return self._receipt(row, target, text=row["text"])

    def remove(self, text, target="memory", **kwargs):
        """Compatibility alias for soft retirement; history is always accessible."""
        return self.archive_entry(text, target, **kwargs)

    @_live
    def commit_revision(self, *, replacements, content, reason, evidence, target="memory",
                        expected_revision=None, actor="curator", source_obs_ids=None, source="curator"):
        """Atomically adopt/merge/replace a proposed set after external evaluation.

        replacements contains entry_id/version pairs. The evaluator runs outside
        this lock; a stale proposal can never delete or overwrite newer knowledge.
        """
        if expected_revision is not None and self._meta["revision"] != expected_revision:
            return self._error("Core changed; re-evaluate the proposal", "version_conflict", revision=self._meta["revision"])
        if not reason.strip() or not evidence:
            return self._error("A revision requires a reason and traceable evidence")
        error = self._validate_text(content)
        if error:
            return self._error(error)
        selected = []
        for item in replacements:
            row, error = self._locate(target, entry_id=item.get("entry_id"), expected_version=item.get("version"))
            if error:
                return error
            if row["source"] == "user_pin" and actor != "user":
                return self._error("Pinned memory cannot be replaced by the agent", "protected")
            if row in selected:
                return self._error("Duplicate replacement identity")
            selected.append(row)
        new = self._new_row(content.strip(), source, meta_extra={"reason": reason, "evidence": evidence,
                           "source_obs_ids": sorted(set(source_obs_ids or []).union(*(set(r.get("source_obs_ids", [])) for r in selected)))})
        new["supersedes_hashes"] = [r["content_hash"] for r in selected]
        selected_ids = {r["entry_id"] for r in selected}
        kept = [r for r in self._meta[target] if r["entry_id"] not in selected_ids]
        if any(r["content_hash"] == new["content_hash"] and r["status"] == "active" for r in kept):
            return self._error("An effective record already contains this content", "duplicate")
        if not self._fits(target, kept + [new]):
            return self._error("Proposal exceeds core capacity; originals preserved", "capacity_blocked", **self.capacity(target))
        for row in selected:
            self._remember(row, target, reason, new["entry_id"])
        self._meta[target] = kept + [new]
        self._save_meta()
        return self._receipt(new, target, disposition="merged" if len(selected) > 1 else "replaced" if selected else "adopted",
                             retired_ids=list(selected_ids))

    @_live
    def adjust_confidence(self, *, entry_hash=None, text_prefix=None, delta, reason, run_id=None,
                          target="memory", archive_after_disproofs=2):
        matches = [r for r in self._meta[target] if r["content_hash"] == entry_hash] if entry_hash else []
        row, error = self._locate(target, text_prefix or "", matches[0]["entry_id"] if len(matches) == 1 else None)
        if error:
            return error
        if row["source"] == "user_pin":
            return self._error("Pinned memory cannot be adjusted by feedback", "protected")
        previous = row["confidence"]
        # Idempotence: one feedback event cannot repeatedly punish/reward an entry.
        if run_id and any(a.get("run_id") == run_id and a.get("reason") == reason for a in row.get("adjustments", [])):
            return self._receipt(row, target, changed=False, previous_confidence=previous)
        new = self._write_confidence(row["source"], previous + delta)
        row["adjustments"].append({"at": _now_iso(), "delta": delta, "reason": reason,
                                   "run_id": run_id, "previous": previous, "new": new})
        row.update(confidence=new, version=row["version"] + 1)
        if delta < 0:
            row["disproof_count"] += 1
            if row["disproof_count"] >= archive_after_disproofs:
                row.update(status="archived", reason=f"disproved: {reason}", confidence=0.0)
            elif new < self._min_inject_confidence:
                row.update(status="candidate", reason=f"reassessment: {reason}")
        self._save_meta()
        return self._receipt(row, target, previous_confidence=previous, disproof_count=row["disproof_count"], entry_hash=row["content_hash"])

    @_live
    def reinforce(self, text, target="memory"):
        row, error = self._locate(target, text)
        if error:
            return error
        if row["status"] == "archived":
            return self._error("Entry is archived")
        # Reading one's own conclusion is usage, not independent evidence.
        row["access_count"] += 1
        row["last_accessed"] = _now_iso()
        self._save_meta()
        return self._receipt(row, target)

    @_live
    def archive_stale(self, target="memory"):
        archived = []
        threshold = ARCHIVE_CONFIDENCE_USER if target == "user" else ARCHIVE_CONFIDENCE_KNOWLEDGE
        changed = False
        for row in self._meta[target]:
            if row["status"] == "archived" or row["source"] == "user_pin":
                continue
            if row["confidence"] < threshold:
                row.update(status="archived", reason="insufficient_confidence", version=row["version"] + 1)
                archived.append(row["text"])
                changed = True
            elif _compute_confidence(_entry_meta_from_dict(row), target) < threshold and not row.get("needs_review"):
                row["needs_review"] = True
                changed = True
        if changed:
            self._save_meta()
        return archived

    @_live
    def archive_inactive(self, target="memory", stale_days=90):
        """Legacy name: inactivity requests review; it does not prove invalidity."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=stale_days)).isoformat()
        changed = False
        for row in self._meta[target]:
            if row["status"] != "archived" and row["source"] != "user_pin" and row["last_accessed"] < cutoff and not row.get("needs_review"):
                row["needs_review"] = True
                changed = True
        if changed:
            self._save_meta()
        return []

    @_live
    def maintenance_marker(self, key, value=None):
        if value is None:
            return self._meta.get(key)
        self._meta[key] = value
        self._save_meta()

    def _scan_threats(self, text):
        return any(re.search(pattern, text.lower()) for pattern in _INJECTION_PATTERNS)

    def _read_file(self, path):
        return path.read_text(encoding="utf-8").strip() if path.is_file() else ""

    def _parse_entries(self, content):
        return [e.strip() for e in content.split("§") if e.strip()]

    def _atomic_write(self, path, content):
        fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def _load_meta(self):
        raw = self._read_file(self._meta_file)
        if not raw and self._previous_file.exists():
            raise ValueError("Memory metadata missing; restore a verified backup before writing")
        try:
            data = json.loads(raw) if raw else {"memory": [], "user": []}
            if not isinstance(data, dict):
                raise ValueError("Invalid memory ledger")
        except (ValueError, OSError) as exc:
            # Atomic replacement makes interrupted writes old-or-new. Malformed
            # committed data is a different fault: silently restoring an older
            # version could discard a newly pinned record. Preserve both files.
            raise ValueError("Memory metadata unreadable; restore a verified backup before writing") from exc
        self._meta = data
        if int(data.get("schema_version", 0)) > 3:
            raise ValueError("Memory ledger requires a newer application version")
        if data.get("schema_version") != 3:
            self._migrate_legacy()
        for target in ("memory", "user"):
            if not self._fits(target, self._meta[target]):
                raise ValueError(f"Effective {target} exceeds configured limit; review before reducing capacity")
        self._sync_projections()

    def _migrate_legacy(self):
        # One durable snapshot before changing meaning or derived files.
        backup = self._memory_dir / ".memory-migration-v2.json"
        if not backup.exists():
            self._atomic_write(backup, json.dumps({"metadata": self._meta,
                "KNOWLEDGE.md": self._read_file(self._memory_file), "USER.md": self._read_file(self._user_file)}, ensure_ascii=False, indent=2))
        for target, path in (("memory", self._memory_file), ("user", self._user_file)):
            old = deepcopy(self._meta.get(target, []))
            entries = self._parse_entries(self._read_file(path))
            rows = []
            for text in entries:
                match = next((r for r in old if _content_hash(r.get("text", "")) == _content_hash(text)), None)
                if match is not None:
                    old.remove(match)
                    row = asdict(_entry_meta_from_dict({**match, "text": text}))
                else:
                    row = self._new_row(text, "reconciled", .4, {"reason": "unverified_provenance"})
                rows.append(row)
            # Metadata also contains authoritative text: never discard it on a count mismatch.
            rows += [asdict(_entry_meta_from_dict(r)) for r in old if r.get("text")]
            for row in rows:
                if row["status"] == "archived":
                    row["reason"] = row["reason"] or "legacy_archived"
                elif row["source"] == "user_pin":
                    row.update(status="active", confidence=1.0)
                elif row["source"] not in _TRUSTED_WRITE_SOURCES or row["confidence"] < self._min_inject_confidence:
                    row.update(status="candidate", reason="unverified_provenance" if row["source"] in ("reconciled", "legacy") else "awaiting_evidence")
                    if row["source"] not in _TRUSTED_WRITE_SOURCES:
                        row["confidence"] = min(row["confidence"], .4)
                else:
                    row["status"] = "active"
                    row["reason"] = row["reason"] or "legacy_admitted"
                    if _compute_confidence(_entry_meta_from_dict(row), target) < self._min_inject_confidence:
                        row["needs_review"] = True
            # Exact duplicates keep their audit identities, with one live representative.
            by_hash = {}
            for row in sorted(rows, key=lambda r: (r["source"] != "user_pin", r["status"] != "active", r["status"] == "archived")):
                h = row["content_hash"]
                if h in by_hash and row["status"] != "archived":
                    winner = by_hash[h]
                    winner["source_obs_ids"] = sorted(set(winner["source_obs_ids"] + row["source_obs_ids"]))
                    row.update(status="archived", reason=f"duplicate_of:{winner['entry_id']}")
                elif row["status"] != "archived":
                    by_hash[h] = row
            self._meta[target] = rows
            if not self._fits(target, rows):
                raise ValueError("Legacy effective core exceeds limit; migration requires explicit review")
        # Preserve older supersession history through normal API access too.
        history = self._meta.setdefault("history", [])
        for old in self._meta.pop("_superseded", []):
            if old.get("text"):
                history.append({**asdict(EntryMeta(text=old["text"], source="legacy", confidence=0,
                                                  status="archived", reason="legacy_superseded")), "target": "memory"})
        self._meta.update(schema_version=3, revision=int(self._meta.get("revision", 0)))
        self._save_meta()

    def _save_meta(self):
        # This private compatibility hook is used by older tests. Production callers
        # mutate only inside _transaction. Reject stale direct writers rather than clobber.
        if not getattr(self._local, "depth", 0):
            with file_transaction(self._lock_file):
                disk = json.loads(self._read_file(self._meta_file) or "{}")
                if disk.get("revision") != self._meta.get("revision"):
                    raise ValueError("Stale memory metadata; reload before updating")
                self._commit()
        else:
            self._commit()

    def _commit(self):
        guard = getattr(self, "_commit_guard", None)
        if guard:
            guard()
        for target in ("memory", "user"):
            self._meta[target] = [asdict(_entry_meta_from_dict(r)) for r in self._meta[target]]
        for target in ("memory", "user"):
            if not self._fits(target, self._meta[target]):
                raise ValueError("Commit exceeds effective memory capacity")
        previous = self._read_file(self._meta_file)
        if previous:
            self._atomic_write(self._previous_file, previous)
        self._meta["revision"] = int(self._meta.get("revision", 0)) + 1
        self._atomic_write(self._meta_file, json.dumps(self._meta, ensure_ascii=False, indent=2))
        # Ledger commit is authoritative. Projection failure cannot roll it back or
        # claim the mutation failed; the next locked read repairs the projection.
        try:
            self._sync_projections()
        except OSError:
            logger.exception("Memory committed; Markdown projection will recover on next read")

    def _sync_projections(self):
        for target, path in (("memory", self._memory_file), ("user", self._user_file)):
            content = ENTRY_DELIMITER.join(r["text"] for r in self._core(target))
            if self._read_file(path) != content:
                self._atomic_write(path, content)
