from __future__ import annotations

import json
import logging
import os
import shutil
import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from trade_compass_agent.llm.providers import ChatMessage

logger = logging.getLogger(__name__)


@dataclass
class SessionMessageRecord:
    role: str
    content: str
    timestamp: datetime | None = None
    sections: list[dict] | None = None
    tool_call_id: str | None = None
    name: str | None = None
    tool_calls: list[dict] = field(default_factory=list)

    def to_chat_message(self) -> ChatMessage:
        return ChatMessage(
            role=self.role,
            content=self.content,
            tool_call_id=self.tool_call_id,
            name=self.name,
            tool_calls=list(self.tool_calls),
        )


@dataclass
class AgentSession:
    session_id: str
    messages: list[SessionMessageRecord]
    created_at: datetime
    updated_at: datetime
    title: str | None = None


@dataclass
class SessionSummary:
    session_id: str
    created_at: datetime
    updated_at: datetime
    message_count: int
    preview: str | None = None
    title: str | None = None


@dataclass
class SessionMessagePage:
    session_id: str
    messages: list[SessionMessageRecord]
    created_at: datetime
    updated_at: datetime
    start_index: int
    total_messages: int
    next_before: int | None
    title: str | None = None


def derive_session_title(message: str, max_len: int = 40) -> str:
    text = " ".join(message.split())
    if not text:
        return "新对话"
    if len(text) <= max_len:
        return text
    return text[: max_len - 1] + "…"


class SessionStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._ensure_dir()
        self._session_locks: dict[str, threading.Lock] = {}
        self._locks_lock = threading.Lock()

    def _get_session_lock(self, session_id: str) -> threading.Lock:
        with self._locks_lock:
            if session_id not in self._session_locks:
                self._session_locks[session_id] = threading.Lock()
            return self._session_locks[session_id]

    def _ensure_dir(self) -> None:
        self.path.mkdir(parents=True, exist_ok=True)

    def _file(self, session_id: str) -> Path:
        return self.path / f"{session_id}.jsonl"

    def _context_file(self, session_id: str) -> Path:
        return self.path / "contexts" / f"{session_id}.jsonl"

    @staticmethod
    def _message_payload(message: SessionMessageRecord, timestamp: datetime) -> dict:
        record: dict = {
            "role": message.role,
            "content": message.content,
            "timestamp": timestamp.isoformat(),
        }
        if message.tool_call_id:
            record["tool_call_id"] = message.tool_call_id
        if message.name:
            record["name"] = message.name
        if message.tool_calls:
            record["tool_calls"] = message.tool_calls
        if message.sections:
            record["sections"] = message.sections
        return record

    def _write_records(
        self,
        file_path: Path,
        session: AgentSession,
        messages: list[SessionMessageRecord],
    ) -> None:
        file_path.parent.mkdir(parents=True, exist_ok=True)
        meta: dict[str, str] = {
            "type": "meta",
            "created_at": session.created_at.isoformat(),
        }
        if session.title:
            meta["title"] = session.title
        now = datetime.now()
        lines = [json.dumps(meta, ensure_ascii=False)]
        lines.extend(
            json.dumps(
                self._message_payload(message, message.timestamp or now),
                ensure_ascii=False,
            )
            for message in messages
        )
        temp_path = file_path.parent / f".{file_path.name}.{uuid4().hex}.tmp"
        temp_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        os.replace(temp_path, file_path)

    def load(self, session_id: str) -> AgentSession | None:
        file_path = self._file(session_id)
        if not file_path.exists():
            return None
        messages: list[SessionMessageRecord] = []
        created_at = datetime.now()
        updated_at = datetime.fromtimestamp(file_path.stat().st_mtime)
        title: str | None = None
        for line in file_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            raw = json.loads(line)
            if raw.get("type") == "meta":
                created_at = datetime.fromisoformat(str(raw.get("created_at", created_at.isoformat())))
                meta_title = raw.get("title")
                if isinstance(meta_title, str) and meta_title.strip():
                    title = meta_title.strip()
                continue
            timestamp_raw = raw.get("timestamp")
            timestamp = (
                datetime.fromisoformat(str(timestamp_raw))
                if isinstance(timestamp_raw, str) and timestamp_raw
                else None
            )
            if timestamp is not None:
                updated_at = max(updated_at, timestamp)
            sections = raw.get("sections")
            messages.append(
                SessionMessageRecord(
                    role=str(raw["role"]),
                    content=str(raw.get("content") or ""),
                    timestamp=timestamp,
                    sections=sections if isinstance(sections, list) else None,
                    tool_call_id=raw.get("tool_call_id"),
                    name=raw.get("name"),
                    tool_calls=raw.get("tool_calls") or [],
                )
            )
        return AgentSession(
            session_id=session_id,
            messages=messages,
            created_at=created_at,
            updated_at=updated_at,
            title=title,
        )

    def load_display_page(
        self,
        session_id: str,
        *,
        limit: int = 50,
        before: int | None = None,
    ) -> SessionMessagePage | None:
        """Load one chronological page without materializing the full transcript."""
        if limit < 1:
            raise ValueError("limit must be positive")
        self._migrate_daily_channel_files(session_id)
        file_path = self._file(session_id)
        if not file_path.exists():
            return None

        page: deque[SessionMessageRecord] = deque(maxlen=limit)
        total_messages = 0
        created_at = datetime.fromtimestamp(file_path.stat().st_ctime)
        updated_at = datetime.fromtimestamp(file_path.stat().st_mtime)
        title: str | None = None
        before_index = max(0, before) if before is not None else None

        lock = self._get_session_lock(session_id)
        with lock, file_path.open(encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                raw = json.loads(line)
                if raw.get("type") == "meta":
                    created_raw = raw.get("created_at")
                    if isinstance(created_raw, str) and created_raw:
                        created_at = datetime.fromisoformat(created_raw)
                    meta_title = raw.get("title")
                    if isinstance(meta_title, str) and meta_title.strip():
                        title = meta_title.strip()
                    continue
                if raw.get("role") not in {"user", "assistant"}:
                    continue

                timestamp_raw = raw.get("timestamp")
                timestamp = (
                    datetime.fromisoformat(str(timestamp_raw))
                    if isinstance(timestamp_raw, str) and timestamp_raw
                    else None
                )
                if timestamp is not None:
                    updated_at = max(updated_at, timestamp)
                if before_index is None or total_messages < before_index:
                    sections = raw.get("sections")
                    page.append(
                        SessionMessageRecord(
                            role=str(raw["role"]),
                            content=str(raw.get("content") or ""),
                            timestamp=timestamp,
                            sections=sections if isinstance(sections, list) else None,
                            tool_call_id=raw.get("tool_call_id"),
                            name=raw.get("name"),
                            tool_calls=raw.get("tool_calls") or [],
                        )
                    )
                total_messages += 1

        end_index = total_messages if before_index is None else min(before_index, total_messages)
        messages = list(page)
        start_index = end_index - len(messages)
        return SessionMessagePage(
            session_id=session_id,
            messages=messages,
            created_at=created_at,
            updated_at=updated_at,
            start_index=start_index,
            total_messages=total_messages,
            next_before=start_index if start_index > 0 else None,
            title=title,
        )

    def load_context(self, session: AgentSession) -> list[SessionMessageRecord]:
        """Load the compact model checkpoint, falling back to the full transcript."""
        file_path = self._context_file(session.session_id)
        if not file_path.exists():
            return list(session.messages)
        messages: list[SessionMessageRecord] = []
        for line in file_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            raw = json.loads(line)
            if raw.get("type") == "meta":
                continue
            timestamp_raw = raw.get("timestamp")
            timestamp = (
                datetime.fromisoformat(str(timestamp_raw))
                if isinstance(timestamp_raw, str) and timestamp_raw
                else None
            )
            sections = raw.get("sections")
            messages.append(
                SessionMessageRecord(
                    role=str(raw["role"]),
                    content=str(raw.get("content") or ""),
                    timestamp=timestamp,
                    sections=sections if isinstance(sections, list) else None,
                    tool_call_id=raw.get("tool_call_id"),
                    name=raw.get("name"),
                    tool_calls=raw.get("tool_calls") or [],
                )
            )
        return messages

    def create(self) -> AgentSession:
        session_id = uuid4().hex
        now = datetime.now()
        session = AgentSession(session_id=session_id, messages=[], created_at=now, updated_at=now)
        self._append_meta(session)
        return session

    def get(self, session_id: str) -> AgentSession | None:
        self._migrate_daily_channel_files(session_id)
        return self.load(session_id)

    def get_or_create(self, session_id: str | None) -> AgentSession:
        if session_id:
            self._migrate_daily_channel_files(session_id)
            loaded = self.load(session_id)
            if loaded is not None:
                return loaded
        if session_id:
            now = datetime.now()
            session = AgentSession(session_id=session_id, messages=[], created_at=now, updated_at=now)
            self._append_meta(session)
            return session
        return self.create()

    def append(self, session: AgentSession, message: SessionMessageRecord) -> None:
        self._ensure_dir()
        lock = self._get_session_lock(session.session_id)
        with lock:
            now = message.timestamp or datetime.now()
            context_file = self._context_file(session.session_id)
            if not context_file.exists():
                self._write_records(context_file, session, session.messages)
            session.messages.append(message)
            session.updated_at = max(session.updated_at, now)
            record = self._message_payload(message, now)
            with self._file(session.session_id).open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            try:
                with context_file.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            except OSError:
                # The transcript remains authoritative. Dropping a damaged
                # checkpoint makes the next turn safely rebuild from it.
                logger.exception("Failed to append model context for %s", session.session_id)
                context_file.unlink(missing_ok=True)

    def replace_context(
        self,
        session: AgentSession,
        messages: list[SessionMessageRecord],
        *,
        archive: bool = True,
    ) -> Path | None:
        """Atomically replace only the model checkpoint; keep the transcript intact."""
        self._ensure_dir()
        lock = self._get_session_lock(session.session_id)
        file_path = self._context_file(session.session_id)
        archived_path: Path | None = None
        with lock:
            if archive and file_path.exists():
                archive_dir = self.path / "archive" / "contexts" / session.session_id
                archive_dir.mkdir(parents=True, exist_ok=True)
                stamp = datetime.now().strftime("%Y%m%dT%H%M%S%f")
                archived_path = archive_dir / f"{stamp}.jsonl"
                shutil.copy2(file_path, archived_path)
            self._write_records(file_path, session, messages)
        return archived_path

    def _migrate_daily_channel_files(self, session_id: str) -> None:
        """Merge obsolete daily channel transcripts into their stable session once."""
        if not session_id.startswith("channel-"):
            return
        daily_files = sorted(self.path.glob(f"{session_id}-????-??-??.jsonl"))
        if not daily_files:
            return

        lock = self._get_session_lock(session_id)
        with lock:
            # Re-check after acquiring the lock because another request may have migrated them.
            daily_files = [path for path in daily_files if path.exists()]
            if not daily_files:
                return
            stable_file = self._file(session_id)
            stable_lines = (
                stable_file.read_text(encoding="utf-8").splitlines()
                if stable_file.exists()
                else []
            )
            meta_line = next(
                (
                    line for line in stable_lines
                    if line.strip() and json.loads(line).get("type") == "meta"
                ),
                None,
            )
            message_lines = [
                line for line in stable_lines
                if line.strip() and json.loads(line).get("type") != "meta"
            ]
            seen = set(message_lines)

            migrated_message_lines: list[str] = []
            for daily_file in daily_files:
                for line in daily_file.read_text(encoding="utf-8").splitlines():
                    if not line.strip():
                        continue
                    raw = json.loads(line)
                    if raw.get("type") == "meta":
                        if meta_line is None:
                            meta_line = line
                        continue
                    if line not in seen:
                        message_lines.append(line)
                        migrated_message_lines.append(line)
                        seen.add(line)

            if meta_line is None:
                meta_line = json.dumps(
                    {"type": "meta", "created_at": datetime.now().isoformat()},
                    ensure_ascii=False,
                )
            temp_path = self.path / f".{session_id}.{uuid4().hex}.tmp"
            temp_path.write_text(
                "\n".join([meta_line, *message_lines]) + "\n",
                encoding="utf-8",
            )
            os.replace(temp_path, stable_file)

            context_file = self._context_file(session_id)
            if context_file.exists() and migrated_message_lines:
                with context_file.open("a", encoding="utf-8") as handle:
                    handle.write("\n".join(migrated_message_lines) + "\n")

            archive_dir = self.path / "archive" / "daily"
            archive_dir.mkdir(parents=True, exist_ok=True)
            for daily_file in daily_files:
                destination = archive_dir / daily_file.name
                if destination.exists():
                    destination = archive_dir / f"{daily_file.stem}-{uuid4().hex}{daily_file.suffix}"
                os.replace(daily_file, destination)

    def _append_meta(self, session: AgentSession) -> None:
        self._ensure_dir()
        meta: dict[str, str] = {"type": "meta", "created_at": session.created_at.isoformat()}
        if session.title:
            meta["title"] = session.title
        with self._file(session.session_id).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(meta, ensure_ascii=False) + "\n")

    def set_title(self, session: AgentSession, title: str) -> None:
        normalized = title.strip()
        if not normalized:
            return
        session.title = normalized
        session.updated_at = datetime.now()
        self._ensure_dir()
        meta = {"type": "meta", "title": normalized}
        lock = self._get_session_lock(session.session_id)
        with lock:
            with self._file(session.session_id).open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(meta, ensure_ascii=False) + "\n")

    def maybe_set_title_from_first_message(self, session: AgentSession, message: str) -> None:
        if session.title:
            return
        user_messages = [m for m in session.messages if m.role == "user"]
        if len(user_messages) != 1:
            return
        self.set_title(session, derive_session_title(message))

    def delete(self, session_id: str) -> bool:
        file_path = self._file(session_id)
        if not file_path.exists():
            return False
        file_path.unlink()
        self._context_file(session_id).unlink(missing_ok=True)
        return True

    def list_recent(self, limit: int = 20, *, exclude_prefix: str | None = None) -> list[SessionSummary]:
        if limit < 1:
            return []
        self._ensure_dir()
        summaries: list[SessionSummary] = []
        for file_path in self.path.glob("*.jsonl"):
            session_id = file_path.stem
            if exclude_prefix and session_id.startswith(exclude_prefix):
                continue
            session = self.load(session_id)
            if session is None:
                continue
            display_messages = [m for m in session.messages if m.role in {"user", "assistant"}]
            preview = next((m.content[:120] for m in display_messages if m.role == "user"), None)
            summaries.append(
                SessionSummary(
                    session_id=session.session_id,
                    created_at=session.created_at,
                    updated_at=session.updated_at,
                    message_count=len(display_messages),
                    preview=preview,
                    title=session.title,
                )
            )
        summaries.sort(key=lambda item: item.updated_at, reverse=True)
        return summaries[:limit]
