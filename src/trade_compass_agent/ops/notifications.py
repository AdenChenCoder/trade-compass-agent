from __future__ import annotations

import json
import subprocess
from datetime import datetime
from pathlib import Path

from trade_compass_agent.config import AppConfig
from trade_compass_agent.domain import Notification


class NotificationCenter:
    def __init__(self, config: AppConfig | None = None, store: "JsonNotificationStore | None" = None) -> None:
        self.config = config
        self.notifications: list[Notification] = []
        self.store = store

    def send(self, notification: Notification) -> None:
        if self.config and not self.config.notifications.enabled:
            return
        self.notifications.append(notification)
        if self.store:
            self.store.append(notification)
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

    def append(self, notification: Notification) -> None:
        from trade_compass_agent.concurrency import atomic_write, get_path_lock

        with get_path_lock(self.path):
            records = self._read_all(self.max_records - 1)
            records.append(notification)
            lines = []
            for item in records[-self.max_records :]:
                lines.append(
                    json.dumps(
                        {
                            "timestamp": datetime.now().isoformat(),
                            "channel": item.channel,
                            "title": item.title,
                            "message": item.message,
                            "severity": item.severity,
                        },
                        ensure_ascii=False,
                    )
                )
            atomic_write(self.path, "\n".join(lines) + "\n")

    def recent(self, limit: int = 30) -> list[Notification]:
        return self._read_all(limit)

    def _read_all(self, limit: int = 500) -> list[Notification]:
        if not self.path.exists():
            return []
        notifications: list[Notification] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                continue
            notifications.append(
                Notification(
                    channel=str(raw.get("channel", "web_log")),
                    title=str(raw.get("title", "")),
                    message=str(raw.get("message", "")),
                    severity=str(raw.get("severity", "info")),
                )
            )
        return notifications[-limit:]


def _escape_applescript(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')
