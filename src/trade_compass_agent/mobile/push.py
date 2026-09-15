"""Per-computer Web Push subscriptions, test delivery and opted-in task reminders."""
import base64
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import time
import threading
from urllib.parse import urlsplit
from uuid import uuid4

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import HTTPException
from pywebpush import webpush, WebPushException
from py_vapid import Vapid, VapidException
import requests

from trade_compass_agent.concurrency import atomic_write, get_path_lock


def validate_subscription(value):
    try:
        endpoint = urlsplit(value["endpoint"])
        host = endpoint.hostname or ""
        allowed = (host == "web.push.apple.com" or host.endswith(".push.apple.com")
                   or host == "fcm.googleapis.com"
                   or host == "updates.push.services.mozilla.com")
        if (endpoint.scheme != "https" or not allowed or endpoint.port not in (None, 443)
                or endpoint.username or endpoint.password or endpoint.fragment
                or len(value["endpoint"]) > 4096):
            raise ValueError()
        def decode(text):
            if not isinstance(text, str) or len(text) > 100:
                raise ValueError()
            return base64.b64decode(text + "=" * (-len(text) % 4), altchars=b"-_", validate=True)
        point = decode(value["keys"]["p256dh"])
        if len(point) != 65 or len(decode(value["keys"]["auth"])) != 16:
            raise ValueError()
        ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), point)
    except (ValueError, KeyError, TypeError) as exc:
        raise HTTPException(422, "当前推送订阅无效或此浏览器的推送服务尚未支持") from exc
    return {"endpoint": value["endpoint"], "keys": {
        "p256dh": value["keys"]["p256dh"], "auth": value["keys"]["auth"]}}


class _NoRedirectSession(requests.Session):
    def post(self, url, **kwargs):
        kwargs["allow_redirects"] = False
        return super().post(url, **kwargs)


class PushStore:
    def __init__(self, directory: Path, origin: str):
        self.origin = origin
        self.key_path = directory / "vapid.pem"
        with get_path_lock(self.key_path):
            if not self.key_path.exists():
                key = ec.generate_private_key(ec.SECP256R1())
                atomic_write(self.key_path, key.private_bytes(serialization.Encoding.PEM,
                    serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode())
            self.key_path.chmod(0o600)
            key = serialization.load_pem_private_key(self.key_path.read_bytes(), password=None)
            if not isinstance(key, ec.EllipticCurvePrivateKey) or not isinstance(key.curve, ec.SECP256R1):
                raise ValueError("Invalid VAPID key; do not replace an existing subscription identity")
        self.public_key = base64.urlsafe_b64encode(key.public_key().public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)).rstrip(b"=").decode()
        self.vapid = Vapid.from_file(str(self.key_path))
        # RFC 8292 permits an HTTPS contact URI with a port. py_vapid's strict
        # subject regex rejects ports; the contact is the validated listener origin
        # or the fixed project homepage for a separately hosted static PWA.
        self.vapid.conf["no-strict"] = True
        self.vapid_headers = {}
        self.vapid_lock = threading.Lock()
        self.path = directory / "push.sqlite3"
        self.path.touch(mode=0o600, exist_ok=True)
        self.path.chmod(0o600)
        with closing(self.connect()) as conn, conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS subscriptions (
                    device_id TEXT PRIMARY KEY, endpoint TEXT UNIQUE, subscription TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS tests (
                    id TEXT PRIMARY KEY, device_id TEXT NOT NULL, created_at REAL NOT NULL,
                    status TEXT NOT NULL, received_at REAL);
            """)
            conn.execute("UPDATE tests SET status='unknown' WHERE status='sending'")
        from trade_compass_agent.mobile.task_push import TaskPushQueue
        self.tasks = TaskPushQueue(self)

    def connect(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def headers(self, endpoint):
        parsed = urlsplit(endpoint)
        audience = f"{parsed.scheme}://{parsed.netloc}"
        with self.vapid_lock:
            saved = self.vapid_headers.get(audience)
            now = time.time()
            if not saved or saved[0] < now:
                signed = self.vapid.sign({"sub": self.origin, "aud": audience, "exp": int(now) + 12 * 3600})
                saved = (now + 3600, signed)
                self.vapid_headers[audience] = saved
            return {**saved[1], "Urgency": "high"}

    def subscribe(self, device_id, value):
        subscription = validate_subscription(value)
        with closing(self.connect()) as conn, conn:
            try:
                conn.execute("INSERT INTO subscriptions VALUES (?, ?, ?) ON CONFLICT(device_id) "
                    "DO UPDATE SET endpoint=excluded.endpoint, subscription=excluded.subscription",
                    (device_id, subscription["endpoint"], json.dumps(subscription)))
            except sqlite3.IntegrityError as exc:
                raise HTTPException(409, "此浏览器订阅已关联另一项配对，请先移除旧连接") from exc

    def unsubscribe(self, device_id, *, endpoint=None):
        with closing(self.connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            if endpoint is not None and not conn.execute("SELECT 1 FROM subscriptions WHERE device_id=? AND endpoint=?",
                                                         (device_id, endpoint)).fetchone():
                return
            conn.execute("DELETE FROM subscriptions WHERE device_id=?", (device_id,))
            self.tasks.cancel(conn, device_id)

    def status(self, device_id):
        with closing(self.connect()) as conn:
            subscribed = conn.execute("SELECT 1 FROM subscriptions WHERE device_id=?", (device_id,)).fetchone()
            latest = conn.execute("SELECT id, status, received_at FROM tests WHERE device_id=? "
                                  "ORDER BY created_at DESC LIMIT 1", (device_id,)).fetchone()
        return {"subscribed": bool(subscribed), "public_key": self.public_key,
                "last_test": dict(latest) if latest else None, **self.tasks.status(device_id)}

    def send_payload(self, subscription, payload, *, ttl, topic=None):
        try:
            with _NoRedirectSession() as session:
                subscription = validate_subscription(subscription)
                headers = self.headers(subscription['endpoint'])
                if topic:
                    headers['Topic'] = topic
                webpush(subscription, data=payload, ttl=ttl, timeout=10,
                        headers=headers, requests_session=session)
            return 'accepted'
        except WebPushException as exc:
            code = exc.response.status_code if exc.response is not None else 0
            if code in (404, 410):
                return 'expired'
            return 'retry' if not code or code in (408, 429) or code >= 500 else 'failed'
        except requests.RequestException:
            return 'retry'
        except (ValueError, VapidException, HTTPException):
            return 'failed'

    def send_test(self, device_id):
        now, test_id = time.time(), uuid4().hex
        with closing(self.connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT subscription FROM subscriptions WHERE device_id=?", (device_id,)).fetchone()
            if row is None:
                raise HTTPException(409, "请先在手机开启通知")
            if conn.execute("SELECT 1 FROM tests WHERE device_id=? AND created_at>?",
                            (device_id, now - 30)).fetchone():
                raise HTTPException(429, "请等待 30 秒后再发送测试通知")
            conn.execute("INSERT INTO tests VALUES (?, ?, ?, 'sending', NULL)", (test_id, device_id, now))
        # Fixed test content, no session text or private research in push payloads.
        payload = json.dumps({"id": test_id, "title": "交易罗盘连接测试",
            "body": "这是你的电脑发送的测试通知。点击返回交易罗盘。"}, ensure_ascii=False)
        subscription = json.loads(row['subscription'])
        status = self.send_payload(subscription, payload, ttl=60)
        if status == 'retry':
            status = 'failed'
        if status == 'expired':
            self.unsubscribe(device_id, endpoint=subscription['endpoint'])
        with closing(self.connect()) as conn, conn:
            conn.execute("UPDATE tests SET status=? WHERE id=?", (status, test_id))
        return {"id": test_id, "status": status}

    def received(self, device_id, test_id):
        with closing(self.connect()) as conn, conn:
            changed = conn.execute("UPDATE tests SET received_at=? WHERE id=? AND device_id=?",
                                   (time.time(), test_id, device_id)).rowcount
        if not changed and not self.tasks.received(device_id, test_id):
            raise HTTPException(404, "通知不存在")
