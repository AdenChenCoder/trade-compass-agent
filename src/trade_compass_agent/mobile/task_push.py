"""A derived, durable push queue. Original task results stay in notifications.jsonl."""
import asyncio
from contextlib import closing
import json
import logging
import math
import time
from uuid import NAMESPACE_URL, uuid5

from fastapi import HTTPException

from trade_compass_agent.config import load_app_config
from trade_compass_agent.ops.notifications import JsonNotificationStore

logger = logging.getLogger(__name__)


class TaskPushQueue:
    def __init__(self, push):
        self.push = push
        with closing(push.connect()) as conn, conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS task_preferences (
                    device_id TEXT PRIMARY KEY, enabled_since REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS task_outbox (
                    id TEXT PRIMARY KEY, event_id TEXT NOT NULL, device_id TEXT NOT NULL,
                    created_at REAL NOT NULL, status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                    next_at REAL NOT NULL, received_at REAL, UNIQUE(event_id, device_id));
                CREATE INDEX IF NOT EXISTS task_outbox_due ON task_outbox(status, next_at);
            """)

    def status(self, device_id):
        with closing(self.push.connect()) as conn:
            enabled = conn.execute("SELECT 1 FROM task_preferences WHERE device_id=?", (device_id,)).fetchone()
            latest = conn.execute("SELECT id, status, received_at FROM task_outbox WHERE device_id=? "
                                  "ORDER BY created_at DESC, rowid DESC LIMIT 1", (device_id,)).fetchone()
        return {"tasks_enabled": bool(enabled), "last_task": dict(latest) if latest else None}

    def set_enabled(self, device_id, enabled):
        with closing(self.push.connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            if enabled:
                if not conn.execute("SELECT 1 FROM subscriptions WHERE device_id=?", (device_id,)).fetchone():
                    raise HTTPException(409, "请先开启并允许手机通知")
                conn.execute("INSERT OR IGNORE INTO task_preferences VALUES (?, ?)", (device_id, time.time()))
            else:
                self.cancel(conn, device_id)

    @staticmethod
    def cancel(conn, device_id):
        conn.execute("DELETE FROM task_preferences WHERE device_id=?", (device_id,))
        conn.execute("UPDATE task_outbox SET status='cancelled' WHERE device_id=? "
                     "AND status IN ('queued', 'retry', 'sending')", (device_id,))

    def reconcile(self, events, devices):
        now = time.time()
        approved = {d['device_id'] for d in devices.list_devices() if d['status'] == 'approved'}
        with closing(self.push.connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            recipients = conn.execute("SELECT p.* FROM task_preferences p "
                                      "JOIN subscriptions s USING(device_id)").fetchall()
            for recipient in recipients:
                device_id = recipient['device_id']
                if device_id not in approved:
                    self.cancel(conn, device_id)
                    continue
                for event in events:
                    created = event.get('created_at')
                    event_id = event.get('event_id')
                    if (not isinstance(event_id, str) or not event_id.startswith('job-run:')
                            or not isinstance(created, (float, int)) or not math.isfinite(created)
                            or not max(recipient['enabled_since'], now - 86400) <= created <= now):
                        continue
                    delivery_id = uuid5(NAMESPACE_URL, f'compass-task:{device_id}:{event_id}').hex
                    conn.execute("INSERT OR IGNORE INTO task_outbox "
                        "(id, event_id, device_id, created_at, status, next_at) VALUES (?, ?, ?, ?, 'queued', ?)",
                        (delivery_id, event_id, device_id, created, now))
            conn.execute("UPDATE task_outbox SET status='expired' WHERE created_at<? "
                         "AND status IN ('queued', 'retry', 'sending')", (now - 86400,))
            # Source events older than 24h are never re-enqueued, so old tombstones can be removed.
            conn.execute("DELETE FROM task_outbox WHERE created_at<?", (now - 7 * 86400,))

    def dispatch_one(self, devices):
        now = time.time()
        with closing(self.push.connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM task_outbox WHERE status IN ('queued', 'retry', 'sending') "
                               "AND next_at<=? ORDER BY next_at, rowid LIMIT 1", (now,)).fetchone()
            if row is None:
                return False
            item = dict(row)
            attempt = item['attempts'] + 1
            conn.execute("UPDATE task_outbox SET status='sending', attempts=?, next_at=? WHERE id=?",
                         (attempt, now + 60, item['id']))
            subscription = conn.execute("SELECT s.subscription FROM subscriptions s "
                "JOIN task_preferences p USING(device_id) WHERE device_id=?", (item['device_id'],)).fetchone()
        approved = any(d['device_id'] == item['device_id'] and d['status'] == 'approved'
                       for d in devices.list_devices())
        invalid_subscription = False
        if not subscription or not approved:
            result = 'cancelled'
        elif now - item['created_at'] >= 86400 or attempt > 24:
            result = 'expired'
        else:
            value = json.loads(subscription['subscription'])
            payload = json.dumps({'id': item['id'], 'kind': 'task', 'title': '交易罗盘任务消息',
                'body': '电脑有一条新的任务结果。打开交易罗盘查看。'}, ensure_ascii=False)
            result = self.push.send_payload(value, payload, ttl=min(3600, int(item['created_at'] + 86400 - now)),
                                            topic=item['id'])
            invalid_subscription = result == 'expired'
        with closing(self.push.connect()) as conn, conn:
            conn.execute("UPDATE task_outbox SET status=?, next_at=? WHERE id=? AND status='sending' AND attempts=?",
                         (result, time.time() + min(3600, 30 * 2 ** min(attempt - 1, 7)), item['id'], attempt))
        if invalid_subscription:
            self.push.unsubscribe(item['device_id'], endpoint=json.loads(subscription['subscription'])['endpoint'])
        return True

    def received(self, device_id, delivery_id):
        with closing(self.push.connect()) as conn, conn:
            return conn.execute("UPDATE task_outbox SET received_at=? WHERE id=? AND device_id=?",
                                (time.time(), delivery_id, device_id)).rowcount


async def run_task_push(push, devices, config, stop):
    source = JsonNotificationStore(config.data_dir / 'notifications.jsonl', config.notifications.max_records)
    while not stop.is_set():
        try:
            current = load_app_config()
            if current.data_dir == config.data_dir and current.notifications.enabled:
                await asyncio.to_thread(push.tasks.reconcile, source.events(source.max_records), devices)
                if not stop.is_set():
                    await asyncio.to_thread(push.tasks.dispatch_one, devices)
        except Exception:
            # Keep the worker alive; never log subscription endpoints or private result content.
            logger.warning('Task notification delivery paused after a local error; will retry')
        try:
            await asyncio.wait_for(stop.wait(), timeout=2)
        except asyncio.TimeoutError:
            pass
