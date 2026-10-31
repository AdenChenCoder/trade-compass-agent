"""Recoverable Codex cursors; application transcripts remain authoritative."""

from __future__ import annotations

import fcntl
import hashlib
import json
from contextlib import contextmanager
from dataclasses import asdict

from trade_compass_agent.concurrency import atomic_write, get_path_lock
from trade_compass_agent.runtime.exceptions import AgentUnavailableError


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    default=str).encode()).hexdigest()


def _transcript_fingerprint(history):
    records = [asdict(message) for message in history]
    for record in records:
        # Preserve compatible cursors written before image references existed.
        if not record.get("image_paths"):
            record.pop("image_paths", None)
    return fingerprint(records)


def _root(data_dir, session_id):
    return data_dir / "native_agents" / "sessions" / fingerprint(session_id)


@contextmanager
def native_session_guard(data_dir, session_id):
    """Reject concurrent writers across HTTP, CLI and separate processes."""
    root = _root(data_dir, session_id)
    root.mkdir(parents=True, exist_ok=True)
    path = root / "session.lock"
    lock = get_path_lock(path)
    if not lock.acquire(blocking=False):
        raise AgentUnavailableError("此会话已有任务正在运行，请等待完成或先停止。")
    try:
        with path.open("a+") as handle:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise AgentUnavailableError("此会话已有任务正在运行，请等待完成或先停止。") from None
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        lock.release()


class NativeConversation:
    def __init__(self, data_dir, session_id, history, *, context=None):
        self.root = _root(data_dir, session_id)
        self.cwd = self.root / "workspace"
        self.cwd.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "cursor.json"
        self.history_digest = _transcript_fingerprint(history)
        self.context_digest = _transcript_fingerprint(history if context is None else context)
        try:
            self.record = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            self.record = {}
        if not isinstance(self.record, dict):
            self.record = {}
        self.pending = None

    def resume_id(self, profile):
        if (self.record.get("status") == "ready"
                and self.record.get("history") == self.history_digest
                and self.record.get("context", self.record.get("history")) == self.context_digest
                and self.record.get("profile") == profile):
            value = self.record.get("thread_id")
            return value if isinstance(value, str) and value else None
        return None

    def begin(self, thread_id, profile):
        # Persist before turn/start: a crash or uncertain external outcome must
        # never turn the next user request into an automatic replay of this turn.
        self.pending = {"thread_id": thread_id, "profile": profile, "status": "pending"}
        atomic_write(self.path, json.dumps(self.pending))

    def commit(self, history, *, context=None):
        # Only the caller that published the final application record may make
        # this cursor reusable. Failure/cancellation leaves it non-reusable.
        if self.pending is not None:
            atomic_write(self.path, json.dumps({**self.pending, "status": "ready",
                         "history": _transcript_fingerprint(history),
                         "context": _transcript_fingerprint(history if context is None else context)}))

    def set_usage_total(self, total):
        # Publish the next accounting baseline only with the completed transcript.
        if self.pending is not None:
            self.pending["usage_total"] = dict(total)

    def abandon(self):
        # A tool-free summary can be published after interrupting a vendor turn.
        # Its unfinished vendor history must not become resumable on commit.
        self.pending = None
