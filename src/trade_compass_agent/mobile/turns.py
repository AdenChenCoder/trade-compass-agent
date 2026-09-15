"""Durable submission receipts; execution remains in the computer's shared agent runtime."""
from __future__ import annotations

import hashlib
import sqlite3
import threading
from contextlib import closing
from pathlib import Path
from uuid import uuid4

from fastapi import HTTPException

from trade_compass_agent.runtime.turn_control import TurnBusyError, get_turn_registry
from trade_compass_agent.web.agent_api import TurnRequest, execute_agent_turn


class MobileTurns:
    def __init__(self, directory: Path):
        self.path = directory / "requests.sqlite3"
        self.path.touch(mode=0o600, exist_ok=True)
        self.path.chmod(0o600)
        self.lock = threading.Lock()
        with closing(self._connect()) as conn, conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS requests (
                device_id TEXT, request_id TEXT, session_id TEXT, content_hash TEXT,
                turn_id TEXT, status TEXT, PRIMARY KEY(device_id, request_id))""")
            # A prior process may have performed side effects. Never replay automatically.
            for row in conn.execute("SELECT turn_id FROM requests WHERE status='running'").fetchall():
                if not get_turn_registry().contains(row["turn_id"]):
                    conn.execute("UPDATE requests SET status='unknown' WHERE turn_id=?", (row["turn_id"],))

    def _connect(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def get(self, device_id: str, request_id: str):
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT * FROM requests WHERE device_id=? AND request_id=?",
                               (device_id, request_id)).fetchone()
        return dict(row) if row else None

    @staticmethod
    def public(record):
        return {k: record[k] for k in ("request_id", "session_id", "turn_id", "status")}

    def submit(self, device_id: str, request_id: str, session_id: str, message: str):
        digest = hashlib.sha256(message.encode()).hexdigest()
        with self.lock:
            previous = self.get(device_id, request_id)
            if previous:
                if previous["session_id"] != session_id or previous["content_hash"] != digest:
                    raise HTTPException(409, "Request ID was already used for a different message")
                return self.public(previous)
            registry = get_turn_registry()
            with closing(self._connect()) as conn:
                if conn.execute("SELECT count(*) FROM requests WHERE status='running'").fetchone()[0] >= 4:
                    raise HTTPException(429, "电脑正在处理多条请求，请稍后发送")
            turn_id = str(uuid4())
            try:
                cancelled = registry.register(turn_id, session_id)
            except TurnBusyError as exc:
                raise HTTPException(409, "会话正在回复，请等待完成后再发送") from exc
            try:
                with closing(self._connect()) as conn, conn:
                    conn.execute("INSERT INTO requests VALUES (?, ?, ?, ?, ?, 'running')",
                                 (device_id, request_id, session_id, digest, turn_id))
                worker = threading.Thread(target=self._run, daemon=True,
                    args=(device_id, request_id, session_id, message, turn_id, cancelled))
                worker.start()
            except Exception:
                registry.unregister(turn_id)
                self._finish(device_id, request_id, "unknown")
                raise
            return {"request_id": request_id, "session_id": session_id,
                    "turn_id": turn_id, "status": "running"}

    def _finish(self, device_id, request_id, status):
        with closing(self._connect()) as conn, conn:
            conn.execute("UPDATE requests SET status=? WHERE device_id=? AND request_id=?",
                         (status, device_id, request_id))

    def _run(self, device_id, request_id, session_id, message, turn_id, cancelled):
        status = "failed"
        try:
            result = execute_agent_turn(TurnRequest(session_id=session_id, message=message),
                                       reserved_turn_id=turn_id, reserved_cancel=cancelled)
            status = "interrupted" if result.interrupted else "completed"
        except Exception:
            # Detailed errors remain in the local trace. A receipt never implies execution twice.
            pass
        finally:
            get_turn_registry().unregister(turn_id)
            self._finish(device_id, request_id, status)
