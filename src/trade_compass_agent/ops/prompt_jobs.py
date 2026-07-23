"""PromptJobStore + PromptJobExecutor — user-created scheduled Agent tasks.

Users create "Prompt Jobs" via Agent chat, CLI, or Web UI. Each Prompt Job
is a prompt + schedule that runs an AgentLoop turn on schedule.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from trade_compass_agent.config import AppConfig
from trade_compass_agent.ops.agent_session import ScheduledAgentSession
from trade_compass_agent.ops.run_store import SqliteRunStore

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class PromptJob:
    id: str
    name: str
    prompt: str
    schedule: str  # "trading_day HH:MM" | cron | "every 2h" | "sat 10:30"
    enabled: bool = True
    trading_day_only: bool = False
    delivery_channels: tuple[str, ...] = ("web_log",)
    created_by: str = "user"  # user | agent | cli
    created_at: datetime | None = None
    updated_at: datetime | None = None


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS prompt_jobs (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    prompt          TEXT NOT NULL,
    schedule        TEXT NOT NULL,
    enabled         INTEGER NOT NULL DEFAULT 1,
    trading_day_only INTEGER NOT NULL DEFAULT 0,
    delivery_json   TEXT NOT NULL DEFAULT '{"channels": ["web_log"]}',
    created_by      TEXT NOT NULL DEFAULT 'user',
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

class PromptJobStore:
    """CRUD for user-created Prompt Jobs. Uses same DB as RunStore."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _init_schema(self) -> None:
        with self._conn() as conn:
            conn.executescript(_SCHEMA)

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(str(self.db_path), timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def create(
        self,
        *,
        name: str,
        prompt: str,
        schedule: str,
        trading_day_only: bool = False,
        delivery_channels: tuple[str, ...] = ("web_log",),
        created_by: str = "user",
    ) -> PromptJob:
        job = PromptJob(
            id=str(uuid4()),
            name=name,
            prompt=prompt,
            schedule=schedule,
            trading_day_only=trading_day_only,
            delivery_channels=delivery_channels,
            created_by=created_by,
            created_at=datetime.now(),
            updated_at=datetime.now(),
        )
        delivery_json = json.dumps({"channels": list(delivery_channels)})
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO prompt_jobs (id, name, prompt, schedule, enabled, trading_day_only,
                   delivery_json, created_by, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (job.id, job.name, job.prompt, job.schedule, 1, int(trading_day_only),
                 delivery_json, created_by,
                 job.created_at.isoformat(), job.updated_at.isoformat()),
            )
        return job

    def get(self, job_id: str) -> PromptJob | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM prompt_jobs WHERE id = ?", (job_id,)).fetchone()
        return _row_to_prompt_job(row) if row else None

    def list_all(self) -> list[PromptJob]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM prompt_jobs ORDER BY created_at DESC").fetchall()
        return [_row_to_prompt_job(r) for r in rows]

    def list_enabled(self) -> list[PromptJob]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM prompt_jobs WHERE enabled = 1 ORDER BY created_at").fetchall()
        return [_row_to_prompt_job(r) for r in rows]

    def update(self, job_id: str, **kwargs) -> PromptJob | None:
        job = self.get(job_id)
        if not job:
            return None

        allowed = {"name", "prompt", "schedule", "enabled", "trading_day_only", "delivery_channels"}
        updates = {k: v for k, v in kwargs.items() if k in allowed and v is not None}
        if not updates:
            return job

        set_parts = []
        params = []
        for k, v in updates.items():
            if k == "delivery_channels":
                set_parts.append("delivery_json = ?")
                params.append(json.dumps({"channels": list(v)}))
            elif k == "enabled":
                set_parts.append("enabled = ?")
                params.append(int(v))
            elif k == "trading_day_only":
                set_parts.append("trading_day_only = ?")
                params.append(int(v))
            else:
                set_parts.append(f"{k} = ?")
                params.append(v)

        set_parts.append("updated_at = ?")
        params.append(datetime.now().isoformat())
        params.append(job_id)

        with self._conn() as conn:
            conn.execute(f"UPDATE prompt_jobs SET {', '.join(set_parts)} WHERE id = ?", params)

        return self.get(job_id)

    def delete(self, job_id: str) -> bool:
        with self._conn() as conn:
            cursor = conn.execute("DELETE FROM prompt_jobs WHERE id = ?", (job_id,))
        return cursor.rowcount > 0

    def set_enabled(self, job_id: str, enabled: bool) -> bool:
        with self._conn() as conn:
            cursor = conn.execute(
                "UPDATE prompt_jobs SET enabled = ?, updated_at = ? WHERE id = ?",
                (int(enabled), datetime.now().isoformat(), job_id),
            )
        return cursor.rowcount > 0


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------

class PromptJobExecutor:
    """Execute a PromptJob by running its prompt through AgentLoop."""

    def __init__(self, config: AppConfig, run_store: SqliteRunStore) -> None:
        self.config = config
        self.run_store = run_store

    def execute(self, job: PromptJob, *, trigger: str = "scheduler") -> None:
        job_key = f"custom:{job.id}"

        if self.run_store.is_job_running(job_key):
            logger.info("Prompt job %s already running (overlap guard)", job.name)
            return

        run = self.run_store.create_run(job_key, trigger=trigger)
        self.run_store.start_run(run)

        try:
            session = ScheduledAgentSession(
                self.config,
                job_id=f"prompt-{job.id}",
            )
            text = session.run(job.prompt)
            self.run_store.complete_run(run, message=text)
        except Exception as exc:
            logger.error("Prompt job %s failed: %s", job.name, exc)
            self.run_store.fail_run(run, error=str(exc), message=f"Prompt job '{job.name}' 执行失败")

        from trade_compass_agent.ops.delivery import DeliveryRouter
        from trade_compass_agent.ops.job_definition import DeliveryConfig
        try:
            delivery = DeliveryConfig(channels=job.delivery_channels)
            DeliveryRouter(self.config).deliver(run, delivery)
        except Exception as exc:
            logger.warning("Prompt job delivery failed: %s", exc)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _row_to_prompt_job(row: sqlite3.Row) -> PromptJob:
    delivery = json.loads(row["delivery_json"] or '{"channels": ["web_log"]}')
    channels = tuple(delivery.get("channels", ["web_log"]))
    return PromptJob(
        id=row["id"],
        name=row["name"],
        prompt=row["prompt"],
        schedule=row["schedule"],
        enabled=bool(row["enabled"]),
        trading_day_only=bool(row["trading_day_only"]),
        delivery_channels=channels,
        created_by=row["created_by"],
        created_at=datetime.fromisoformat(row["created_at"]) if row["created_at"] else None,
        updated_at=datetime.fromisoformat(row["updated_at"]) if row["updated_at"] else None,
    )
