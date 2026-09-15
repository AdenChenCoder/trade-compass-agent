import json
from pathlib import Path

from trade_compass_agent.config import AppConfig
from trade_compass_agent.domain import Notification
from trade_compass_agent.ops.notifications import JsonNotificationStore, NotificationCenter


def test_json_notification_store_round_trip(tmp_path: Path):
    store = JsonNotificationStore(tmp_path / "notifications.jsonl", max_records=10)
    store.append(Notification(channel="scheduler", title="done", message="ok", severity="info"))
    recent = store.recent(5)
    assert len(recent) == 1
    assert recent[0].title == "done"


def test_notification_center_writes_store(tmp_path: Path):
    config = AppConfig(data_dir=tmp_path, memory_dir=tmp_path)
    store = JsonNotificationStore(tmp_path / "notifications.jsonl")
    center = NotificationCenter(config, store=store)
    center.send(Notification(channel="manual", title="hello", message="world", severity="info"))
    assert store.recent(1)[0].message == "world"


def test_legacy_notifications_remain_readable_without_rewriting_history(tmp_path: Path):
    path = tmp_path / "notifications.jsonl"
    legacy = {"channel": "scheduler:old", "title": "定时任务失败: old", "message": "原始内容", "severity": "warning"}
    path.write_text(json.dumps(legacy) + "\n")
    original = path.read_bytes()
    notice = JsonNotificationStore(path).recent()[0]
    assert notice.task_status is None
    assert notice.message == "原始内容" and notice.severity == "warning"
    assert path.read_bytes() == original
