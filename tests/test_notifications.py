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
