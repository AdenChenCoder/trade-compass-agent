from __future__ import annotations

import json
import subprocess
import time
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from trade_compass_agent.config import AppConfig
from trade_compass_agent.domain import Notification


class NotificationCenter:
    def __init__(self, config: AppConfig | None = None, store: "JsonNotificationStore | None" = None) -> None:
        self.config = config
        self.notifications: list[Notification] = []
        self.store = store

    def send(self, notification: Notification, *, event_id: str | None = None) -> None:
        if self.config and not self.config.notifications.enabled:
            return
        self.notifications.append(notification)
        if self.store:
            self.store.append(notification, event_id=event_id)
        if self.config and self.config.notifications.macos_enabled:
            self._send_macos(notification)

    def _send_macos(self, notification: Notification) -> None:
        title = f"交易罗盘 · {notification.severity.upper()}"
        body = f"{notification.title} - {notification.message}"
        script = f'display notification "{_escape_applescript(body)}" with title "{_escape_applescript(title)}"'
        try:
            subprocess.run(["osascript", "-e", script], check=False, capture_output=True)
        except Exception:
            # macOS notifications are best-effort only.
            pass


class JsonNotificationStore:
    def __init__(self, path: Path, max_records: int = 500) -> None:
        self.path = path
        self.max_records = max(max_records, 100)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, notification: Notification, *, event_id: str | None = None) -> None:
        from trade_compass_agent.concurrency import atomic_write, get_path_lock

        with get_path_lock(self.path):
            # Preserve previous timestamps and event identities when rotating the log.
            records = self.events(self.max_records)
            previous = next((item for item in records if event_id and item.get('event_id') == event_id), {})
            records.append({"timestamp": previous.get('timestamp', datetime.now().isoformat()),
                "created_at": previous.get('created_at', time.time()),
                "event_id": event_id or uuid4().hex, "channel": notification.channel,
                "title": notification.title, "message": notification.message, "severity": notification.severity,
                "task_status": notification.task_status})
            lines = [json.dumps(item, ensure_ascii=False) for item in records[-self.max_records:]]
            atomic_write(self.path, "\n".join(lines) + "\n")

    def recent(self, limit: int = 30) -> list[Notification]:
        return self._read_all(limit)

    def _read_all(self, limit: int = 500) -> list[Notification]:
        return [Notification(channel=str(raw.get("channel", "web_log")), title=str(raw.get("title", "")),
            message=str(raw.get("message", "")), severity=str(raw.get("severity", "info")),
            task_status=raw.get("task_status") if isinstance(raw.get("task_status"), str) else None)
            for raw in self.events(limit)]

    def events(self, limit: int = 500) -> list[dict]:
        if not self.path.exists():
            return []
        records = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(raw, dict):
                records.append(raw)
        return records[-limit:]


def _escape_applescript(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')
