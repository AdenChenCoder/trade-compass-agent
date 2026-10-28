"""Durable, leased job-result delivery. Analysis and orders are never replayed."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from contextlib import contextmanager

MAX_ATTEMPTS = 3
RETRY_DELAYS = (60, 300)
LEASE_SECONDS = 120


class DeliveryStore:
    def __init__(self, path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS job_deliveries (
                id TEXT PRIMARY KEY, run_id TEXT NOT NULL, channel TEXT NOT NULL,
                payload_json TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL DEFAULT 0,
                lease_until REAL NOT NULL DEFAULT 0, last_error TEXT NOT NULL DEFAULT '',
                UNIQUE(run_id, channel))""")

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def enqueue(self, run_id, channel, payload):
        identity = hashlib.sha256(f"{run_id}:{channel}".encode()).hexdigest()[:32]
        with self._conn() as conn:
            conn.execute("INSERT OR IGNORE INTO job_deliveries (id, run_id, channel, payload_json) VALUES (?, ?, ?, ?)",
                         (identity, run_id, channel, json.dumps(payload, ensure_ascii=False)))
        return identity

    def claim(self, identity=None, *, now=None):
        now = time.time() if now is None else now
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            # A crash during the final attempt must not leave an immortal lease.
            conn.execute("UPDATE job_deliveries SET status='failed', last_error='投递中断，已达到重试上限' "
                         "WHERE status='sending' AND lease_until<=? AND attempts>=?", (now, MAX_ATTEMPTS))
            row = conn.execute("SELECT * FROM job_deliveries WHERE "
                "((status IN ('pending','retry') AND next_attempt<=?) OR (status='sending' AND lease_until<=?)) "
                "AND attempts<?" + (" AND id=?" if identity else "") + " ORDER BY next_attempt, rowid LIMIT 1",
                (now, now, MAX_ATTEMPTS, *([identity] if identity else []))).fetchone()
            if row is None:
                return None
            conn.execute("UPDATE job_deliveries SET status='sending', attempts=attempts+1, lease_until=? WHERE id=?",
                         (now + LEASE_SECONDS, row["id"]))
            return {**dict(row), "attempts": row["attempts"] + 1}

    def has_due(self, *, now=None):
        now = time.time() if now is None else now
        with self._conn() as conn:
            return conn.execute("SELECT 1 FROM job_deliveries WHERE "
                "(status IN ('pending','retry') AND next_attempt<=?) OR (status='sending' AND lease_until<=?) LIMIT 1",
                (now, now)).fetchone() is not None

    def finish(self, claimed, *, sent=False, skipped=False, error="", now=None):
        from trade_compass_agent.logging_config import redact_log_text
        now = time.time() if now is None else now
        attempt = claimed["attempts"]
        state = "completed" if sent else "skipped" if skipped else "retry" if attempt < MAX_ATTEMPTS else "failed"
        due = now + RETRY_DELAYS[min(attempt - 1, len(RETRY_DELAYS) - 1)] if state == "retry" else 0
        with self._conn() as conn:
            conn.execute("UPDATE job_deliveries SET status=?, next_attempt=?, lease_until=0, last_error=? "
                         "WHERE id=? AND status='sending' AND attempts=?",
                         (state, due, redact_log_text(error), claimed["id"], attempt))
        return state

    def for_run(self, run_id):
        with self._conn() as conn:
            return [{k: row[k] for k in ("id", "channel", "status", "attempts", "next_attempt", "last_error")}
                    for row in conn.execute("SELECT * FROM job_deliveries WHERE run_id=? ORDER BY rowid", (run_id,))]
