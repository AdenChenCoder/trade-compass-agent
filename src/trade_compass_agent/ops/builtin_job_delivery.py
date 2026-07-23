"""Persist per-job delivery channel overrides for built-in scheduler jobs."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS builtin_job_delivery (
    job_id          TEXT PRIMARY KEY,
    delivery_json   TEXT NOT NULL
);
"""


class BuiltinJobDeliveryStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as conn:
            conn.executescript(_SCHEMA)

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(str(self.db_path), timeout=10)
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def get(self, job_id: str) -> tuple[str, ...] | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT delivery_json FROM builtin_job_delivery WHERE job_id = ?",
                (job_id,),
            ).fetchone()
        if not row:
            return None
        payload = json.loads(row[0] or '{"channels": ["web_log"]}')
        return tuple(payload.get("channels", ["web_log"]))

    def set(self, job_id: str, channels: tuple[str, ...]) -> None:
        payload = json.dumps({"channels": list(channels)}, ensure_ascii=False)
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO builtin_job_delivery (job_id, delivery_json)
                   VALUES (?, ?)
                   ON CONFLICT(job_id) DO UPDATE SET delivery_json = excluded.delivery_json""",
                (job_id, payload),
            )

    def all(self) -> dict[str, tuple[str, ...]]:
        with self._conn() as conn:
            rows = conn.execute("SELECT job_id, delivery_json FROM builtin_job_delivery").fetchall()
        result: dict[str, tuple[str, ...]] = {}
        for job_id, raw in rows:
            payload = json.loads(raw or '{"channels": ["web_log"]}')
            result[job_id] = tuple(payload.get("channels", ["web_log"]))
        return result
