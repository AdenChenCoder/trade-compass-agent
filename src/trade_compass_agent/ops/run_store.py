"""SqliteRunStore — Job and step run persistence.

Replaces JsonJobRunStore with SQLite for indexed queries and atomic operations.
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

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class RunRecord:
    id: str
    job_id: str
    trigger: str  # scheduler | api | cli | agent
    status: str  # queued | running | completed | degraded | failed | skipped | timed_out
    started_at: datetime | None = None
    finished_at: datetime | None = None
    message: str = ""
    artifact: str | None = None
    error: str | None = None
    created_at: datetime | None = None

    @property
    def ok(self) -> bool:
        return self.status == "completed"


@dataclass
class StepRunRecord:
    id: str
    run_id: str
    step_id: str
    status: str  # pending | running | completed | failed | timed_out
    started_at: datetime | None = None
    finished_at: datetime | None = None
    output: str | None = None
    error: str | None = None
    data_json: str | None = None


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS job_runs (
    id          TEXT PRIMARY KEY,
    job_id      TEXT NOT NULL,
    trigger     TEXT NOT NULL DEFAULT 'scheduler',
    status      TEXT NOT NULL DEFAULT 'queued',
    started_at  TEXT,
    finished_at TEXT,
    message     TEXT DEFAULT '',
    artifact    TEXT,
    error       TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS step_runs (
    id          TEXT PRIMARY KEY,
    run_id      TEXT NOT NULL REFERENCES job_runs(id),
    step_id     TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending',
    started_at  TEXT,
    finished_at TEXT,
    output      TEXT,
    error       TEXT,
    data_json   TEXT
);

CREATE INDEX IF NOT EXISTS idx_runs_job_status ON job_runs(job_id, status);
CREATE INDEX IF NOT EXISTS idx_runs_created ON job_runs(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_step_runs_run ON step_runs(run_id);
"""


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

class SqliteRunStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _init_schema(self) -> None:
        with self._conn() as conn:
            conn.executescript(_SCHEMA)
            # Migration: add data_json column if missing
            cols = {row["name"] for row in conn.execute("PRAGMA table_info(step_runs)").fetchall()}
            if "data_json" not in cols:
                conn.execute("ALTER TABLE step_runs ADD COLUMN data_json TEXT")

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(str(self.db_path), timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # -- Job Runs --

    def create_run(self, job_id: str, trigger: str = "scheduler") -> RunRecord:
        run = RunRecord(
            id=str(uuid4()),
            job_id=job_id,
            trigger=trigger,
            status="queued",
            created_at=datetime.now(),
        )
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO job_runs (id, job_id, trigger, status, created_at) VALUES (?, ?, ?, ?, ?)",
                (run.id, run.job_id, run.trigger, run.status, _dt(run.created_at)),
            )
        return run

    def start_run(self, run: RunRecord) -> None:
        run.status = "running"
        run.started_at = datetime.now()
        with self._conn() as conn:
            conn.execute(
                "UPDATE job_runs SET status = 'running', started_at = ? WHERE id = ?",
                (_dt(run.started_at), run.id),
            )

    def complete_run(self, run: RunRecord, *, message: str = "", artifact: str | None = None) -> None:
        run.status = "completed"
        run.finished_at = datetime.now()
        run.message = message
        run.artifact = artifact
        with self._conn() as conn:
            conn.execute(
                "UPDATE job_runs SET status = 'completed', finished_at = ?, message = ?, artifact = ?, error = NULL WHERE id = ?",
                (_dt(run.finished_at), message, artifact, run.id),
            )

    def fail_run(self, run: RunRecord, *, error: str, message: str = "") -> None:
        run.status = "failed"
        run.finished_at = datetime.now()
        run.error = error
        run.message = message
        with self._conn() as conn:
            conn.execute(
                "UPDATE job_runs SET status = 'failed', finished_at = ?, error = ?, message = ? WHERE id = ?",
                (_dt(run.finished_at), error, message, run.id),
            )

    def degrade_run(
        self,
        run: RunRecord,
        *,
        error: str,
        message: str = "",
        artifact: str | None = None,
    ) -> None:
        run.status = "degraded"
        run.finished_at = datetime.now()
        run.error = error
        run.message = message
        run.artifact = artifact
        with self._conn() as conn:
            conn.execute(
                "UPDATE job_runs SET status = 'degraded', finished_at = ?, error = ?, "
                "message = ?, artifact = ? WHERE id = ?",
                (_dt(run.finished_at), error, message, artifact, run.id),
            )

    def skip_run(self, run: RunRecord, *, reason: str) -> None:
        run.status = "skipped"
        run.finished_at = datetime.now()
        run.message = reason
        with self._conn() as conn:
            conn.execute(
                "UPDATE job_runs SET status = 'skipped', finished_at = ?, message = ? WHERE id = ?",
                (_dt(run.finished_at), reason, run.id),
            )

    def timeout_run(self, run: RunRecord) -> None:
        run.status = "timed_out"
        run.finished_at = datetime.now()
        run.error = "Job execution timed out"
        with self._conn() as conn:
            conn.execute(
                "UPDATE job_runs SET status = 'timed_out', finished_at = ?, error = ? WHERE id = ?",
                (_dt(run.finished_at), run.error, run.id),
            )

    def recent_runs(self, limit: int = 20, job_id: str | None = None) -> list[RunRecord]:
        with self._conn() as conn:
            if job_id:
                rows = conn.execute(
                    "SELECT * FROM job_runs WHERE job_id = ? ORDER BY created_at DESC LIMIT ?",
                    (job_id, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM job_runs ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        return [_row_to_run(r) for r in rows]

    def get_run(self, run_id: str) -> RunRecord | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM job_runs WHERE id = ?", (run_id,)).fetchone()
        return _row_to_run(row) if row else None

    def is_job_running(self, job_id: str) -> bool:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM job_runs WHERE job_id = ? AND status = 'running'",
                (job_id,),
            ).fetchone()
        return row[0] > 0

    def reap_stale_runs(
        self,
        job_timeouts: dict[str, int],
        *,
        grace_seconds: int = 300,
        now: datetime | None = None,
    ) -> list[str]:
        """Mark orphaned runs stuck in ``running`` past job timeout as failed.

        Process restarts during agent steps leave ``running`` rows in SQLite;
        overlap guard then blocks future scheduled runs until they are cleared.
        """
        ts = now or datetime.now()
        reaped: list[str] = []
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT id, job_id, started_at FROM job_runs WHERE status = 'running'",
            ).fetchall()
            for row in rows:
                started = _parse_dt(row["started_at"])
                if started is None:
                    continue
                limit = job_timeouts.get(row["job_id"], 600) + grace_seconds
                if (ts - started).total_seconds() <= limit:
                    continue
                reaped.extend(
                    self._mark_run_interrupted(conn, row["id"], ts, reason="exceeded job timeout")
                )
        if reaped:
            logger.warning("Reaped %d stale job run(s): %s", len(reaped), reaped)
        return reaped

    def reap_interrupted_runs(self, *, now: datetime | None = None) -> list[str]:
        """Mark every ``running`` run as failed — call on scheduler/process startup.

        A fresh process cannot be executing runs left over from a crashed predecessor.
        """
        ts = now or datetime.now()
        reaped: list[str] = []
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT id FROM job_runs WHERE status = 'running'",
            ).fetchall()
            for row in rows:
                reaped.extend(
                    self._mark_run_interrupted(
                        conn, row["id"], ts, reason="process restarted while job was running",
                    )
                )
        if reaped:
            logger.warning(
                "Reaped %d interrupted job run(s) on startup: %s", len(reaped), reaped,
            )
        return reaped

    def reap_orphan_step_runs(self, *, now: datetime | None = None) -> int:
        """Close ``running`` step rows whose parent run is no longer running."""
        ts = now or datetime.now()
        with self._conn() as conn:
            cur = conn.execute(
                """UPDATE step_runs
                   SET status = 'failed', finished_at = ?, error = ?
                   WHERE status = 'running'
                     AND run_id IN (
                         SELECT sr.run_id
                         FROM step_runs sr
                         LEFT JOIN job_runs jr ON jr.id = sr.run_id
                         WHERE sr.status = 'running'
                           AND (jr.id IS NULL OR jr.status != 'running')
                     )""",
                (_dt(ts), "orphaned step reaped: parent run is not running"),
            )
            count = cur.rowcount
        if count:
            logger.warning("Reaped %d orphaned step run(s)", count)
        return count

    def _mark_run_interrupted(
        self,
        conn: sqlite3.Connection,
        run_id: str,
        ts: datetime,
        *,
        reason: str,
    ) -> list[str]:
        error = f"stale run reaped: {reason}"
        cur = conn.execute(
            "UPDATE job_runs SET status = 'failed', finished_at = ?, error = ?, message = ? "
            "WHERE id = ? AND status = 'running'",
            (_dt(ts), error, "orphaned run cleaned up", run_id),
        )
        if cur.rowcount == 0:
            return []
        conn.execute(
            "UPDATE step_runs SET status = 'failed', finished_at = ?, error = ? "
            "WHERE run_id = ? AND status = 'running'",
            (_dt(ts), "orphaned step reaped", run_id),
        )
        return [run_id]

    # -- Step Runs --

    def create_step_run(self, run_id: str, step_id: str) -> StepRunRecord:
        rec = StepRunRecord(id=str(uuid4()), run_id=run_id, step_id=step_id, status="pending")
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO step_runs (id, run_id, step_id, status) VALUES (?, ?, ?, ?)",
                (rec.id, rec.run_id, rec.step_id, rec.status),
            )
        return rec

    def start_step(self, rec: StepRunRecord) -> None:
        rec.status = "running"
        rec.started_at = datetime.now()
        with self._conn() as conn:
            conn.execute(
                "UPDATE step_runs SET status = 'running', started_at = ? WHERE id = ?",
                (_dt(rec.started_at), rec.id),
            )

    def complete_step(self, rec: StepRunRecord, output: str = "", data_json: str | None = None) -> None:
        rec.status = "completed"
        rec.finished_at = datetime.now()
        rec.output = output
        rec.data_json = data_json
        with self._conn() as conn:
            conn.execute(
                "UPDATE step_runs SET status = 'completed', finished_at = ?, output = ?, data_json = ? WHERE id = ?",
                (_dt(rec.finished_at), output, data_json, rec.id),
            )

    def fail_step(self, rec: StepRunRecord, error: str) -> None:
        rec.status = "failed"
        rec.finished_at = datetime.now()
        rec.error = error
        with self._conn() as conn:
            conn.execute(
                "UPDATE step_runs SET status = 'failed', finished_at = ?, error = ? WHERE id = ?",
                (_dt(rec.finished_at), error, rec.id),
            )

    def step_runs_for(self, run_id: str) -> list[StepRunRecord]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM step_runs WHERE run_id = ? ORDER BY started_at",
                (run_id,),
            ).fetchall()
        return [_row_to_step(r) for r in rows]

    # -- Migration helper --

    def import_from_jsonl(self, jsonl_path: Path) -> int:
        """One-time import from legacy JsonJobRunStore JSONL file."""
        if not jsonl_path.exists():
            return 0
        count = 0
        with self._conn() as conn:
            for line in jsonl_path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                raw = json.loads(line)
                conn.execute(
                    """INSERT OR IGNORE INTO job_runs
                       (id, job_id, trigger, status, started_at, finished_at, message, artifact, error, created_at)
                       VALUES (?, ?, 'scheduler', ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        raw["id"],
                        raw["job_id"],
                        "completed" if raw.get("ok") else "failed",
                        raw.get("started_at"),
                        raw.get("finished_at"),
                        raw.get("message", ""),
                        raw.get("artifact"),
                        raw.get("error"),
                        raw.get("started_at"),
                    ),
                )
                count += 1
        logger.info("Imported %d legacy runs from %s", count, jsonl_path)
        return count


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _dt(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    return datetime.fromisoformat(s)


def _row_to_run(row: sqlite3.Row) -> RunRecord:
    return RunRecord(
        id=row["id"],
        job_id=row["job_id"],
        trigger=row["trigger"],
        status=row["status"],
        started_at=_parse_dt(row["started_at"]),
        finished_at=_parse_dt(row["finished_at"]),
        message=row["message"] or "",
        artifact=row["artifact"],
        error=row["error"],
        created_at=_parse_dt(row["created_at"]),
    )


def _row_to_step(row: sqlite3.Row) -> StepRunRecord:
    return StepRunRecord(
        id=row["id"],
        run_id=row["run_id"],
        step_id=row["step_id"],
        status=row["status"],
        started_at=_parse_dt(row["started_at"]),
        finished_at=_parse_dt(row["finished_at"]),
        output=row["output"],
        error=row["error"],
        data_json=row["data_json"] if "data_json" in row.keys() else None,
    )
