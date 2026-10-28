"""Persistent, request-level LLM usage audit records."""

from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LLMUsageContext:
    job_id: str = ""
    run_id: str = ""
    session_id: str = ""
    attempt: int = 1


_USAGE_CONTEXT: ContextVar[LLMUsageContext] = ContextVar(
    "trade_compass_llm_usage_context",
    default=LLMUsageContext(),
)


def current_llm_usage_context() -> LLMUsageContext:
    return _USAGE_CONTEXT.get()


@contextmanager
def llm_usage_context(
    *,
    job_id: str | None = None,
    run_id: str | None = None,
    session_id: str | None = None,
    attempt: int | None = None,
) -> Iterator[LLMUsageContext]:
    """Add request attribution while preserving fields owned by an outer scope."""

    parent = current_llm_usage_context()
    value = LLMUsageContext(
        job_id=parent.job_id if job_id is None else str(job_id),
        run_id=parent.run_id if run_id is None else str(run_id),
        session_id=parent.session_id if session_id is None else str(session_id),
        attempt=parent.attempt if attempt is None else max(1, int(attempt)),
    )
    token = _USAGE_CONTEXT.set(value)
    try:
        yield value
    finally:
        _USAGE_CONTEXT.reset(token)


@dataclass(frozen=True)
class ProviderTokenUsage:
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    cached_tokens: int | None = None
    total_tokens: int | None = None
    billed_amount: float | None = None
    billed_currency: str = ""


@dataclass(frozen=True)
class LLMUsageRecord:
    id: str
    usage_date: str
    started_at: str
    finished_at: str
    provider: str
    model: str
    operation: str
    job_id: str
    run_id: str
    session_id: str
    attempt: int
    sdk_attempts: int
    status: str
    prompt_tokens: int | None
    completion_tokens: int | None
    cached_tokens: int | None
    total_tokens: int | None
    billed_amount: float | None
    billed_currency: str
    cost_source: str
    provider_request_id: str
    duration_ms: int
    error_type: str


_SCHEMA = """
CREATE TABLE IF NOT EXISTS llm_usage_calls (
    id                  TEXT PRIMARY KEY,
    usage_date          TEXT NOT NULL,
    started_at          TEXT NOT NULL,
    finished_at         TEXT NOT NULL,
    provider            TEXT NOT NULL,
    model               TEXT NOT NULL,
    operation           TEXT NOT NULL,
    job_id              TEXT NOT NULL DEFAULT '',
    run_id              TEXT NOT NULL DEFAULT '',
    session_id          TEXT NOT NULL DEFAULT '',
    attempt             INTEGER NOT NULL DEFAULT 1,
    sdk_attempts        INTEGER NOT NULL DEFAULT 1,
    status              TEXT NOT NULL,
    prompt_tokens       INTEGER,
    completion_tokens   INTEGER,
    cached_tokens       INTEGER,
    total_tokens        INTEGER,
    billed_amount       REAL,
    billed_currency     TEXT NOT NULL DEFAULT '',
    cost_source         TEXT NOT NULL DEFAULT 'unavailable',
    provider_request_id TEXT NOT NULL DEFAULT '',
    duration_ms         INTEGER NOT NULL DEFAULT 0,
    error_type          TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_llm_usage_date ON llm_usage_calls(usage_date);
CREATE INDEX IF NOT EXISTS idx_llm_usage_model ON llm_usage_calls(model);
CREATE INDEX IF NOT EXISTS idx_llm_usage_job ON llm_usage_calls(job_id);
CREATE INDEX IF NOT EXISTS idx_llm_usage_session ON llm_usage_calls(session_id);
"""


class SqliteLLMUsageStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as conn:
            conn.executescript(_SCHEMA)

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(str(self.path), timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def record(self, record: LLMUsageRecord) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO llm_usage_calls ("
                "id, usage_date, started_at, finished_at, provider, model, operation, "
                "job_id, run_id, session_id, attempt, sdk_attempts, status, prompt_tokens, "
                "completion_tokens, cached_tokens, total_tokens, billed_amount, billed_currency, "
                "cost_source, provider_request_id, duration_ms, error_type"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    record.id,
                    record.usage_date,
                    record.started_at,
                    record.finished_at,
                    record.provider,
                    record.model,
                    record.operation,
                    record.job_id,
                    record.run_id,
                    record.session_id,
                    record.attempt,
                    record.sdk_attempts,
                    record.status,
                    record.prompt_tokens,
                    record.completion_tokens,
                    record.cached_tokens,
                    record.total_tokens,
                    record.billed_amount,
                    record.billed_currency,
                    record.cost_source,
                    record.provider_request_id,
                    record.duration_ms,
                    record.error_type,
                ),
            )

    def recent(
        self,
        *,
        limit: int = 100,
        date_from: date | None = None,
        date_to: date | None = None,
        model: str | None = None,
        job_id: str | None = None,
    ) -> list[LLMUsageRecord]:
        where, values = _filters(date_from=date_from, date_to=date_to, model=model, job_id=job_id)
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT * FROM llm_usage_calls{where} ORDER BY started_at DESC LIMIT ?",
                (*values, max(1, int(limit))),
            ).fetchall()
        return [_row_to_record(row) for row in rows]

    def aggregate(
        self,
        group_by: str | None = None,
        *,
        date_from: date | None = None,
        date_to: date | None = None,
        model: str | None = None,
        job_id: str | None = None,
    ) -> list[dict[str, Any]]:
        expressions = {
            None: "'all'",
            "date": "usage_date",
            "model": "model",
            "job": (
                "CASE WHEN job_id <> '' THEN job_id "
                "WHEN session_id <> '' THEN 'interactive' ELSE 'unattributed' END"
            ),
        }
        if group_by not in expressions:
            raise ValueError(f"Unsupported LLM usage grouping: {group_by}")
        group_expr = expressions[group_by]
        where, values = _filters(date_from=date_from, date_to=date_to, model=model, job_id=job_id)
        group_sql = " GROUP BY group_key" if group_by is not None else ""
        order_sql = " ORDER BY group_key DESC" if group_by == "date" else " ORDER BY group_key"
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT {group_expr} AS group_key, COUNT(*) AS request_count, "
                "COALESCE(SUM(sdk_attempts), 0) AS sdk_attempt_count, "
                "COALESCE(SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END), 0) AS completed_count, "
                "COALESCE(SUM(CASE WHEN status <> 'completed' THEN 1 ELSE 0 END), 0) AS failed_count, "
                "COALESCE(SUM(CASE WHEN total_tokens IS NOT NULL THEN 1 ELSE 0 END), 0) AS measured_count, "
                "COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens, "
                "COALESCE(SUM(completion_tokens), 0) AS completion_tokens, "
                "COALESCE(SUM(cached_tokens), 0) AS cached_tokens, "
                "COALESCE(SUM(total_tokens), 0) AS total_tokens "
                f"FROM llm_usage_calls{where}{group_sql}{order_sql}",
                values,
            ).fetchall()
            cost_rows = conn.execute(
                f"SELECT {group_expr} AS group_key, "
                "CASE WHEN billed_currency <> '' THEN billed_currency ELSE 'unspecified' END AS currency, "
                "SUM(billed_amount) AS amount FROM llm_usage_calls"
                f"{_append_condition(where, 'billed_amount IS NOT NULL')} GROUP BY group_key, currency",
                values,
            ).fetchall()
        costs: dict[str, dict[str, float]] = {}
        for row in cost_rows:
            costs.setdefault(str(row["group_key"]), {})[str(row["currency"])] = round(float(row["amount"]), 8)
        return [
            {
                "key": str(row["group_key"]),
                "request_count": int(row["request_count"]),
                "sdk_attempt_count": int(row["sdk_attempt_count"]),
                "completed_count": int(row["completed_count"]),
                "failed_count": int(row["failed_count"]),
                "measured_count": int(row["measured_count"]),
                "prompt_tokens": int(row["prompt_tokens"]),
                "completion_tokens": int(row["completion_tokens"]),
                "cached_tokens": int(row["cached_tokens"]),
                "total_tokens": int(row["total_tokens"]),
                "provider_reported_costs": costs.get(str(row["group_key"]), {}),
            }
            for row in rows
        ]


def _filters(
    *,
    date_from: date | None,
    date_to: date | None,
    model: str | None,
    job_id: str | None,
) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    values: list[Any] = []
    if date_from is not None:
        clauses.append("usage_date >= ?")
        values.append(date_from.isoformat())
    if date_to is not None:
        clauses.append("usage_date <= ?")
        values.append(date_to.isoformat())
    if model:
        clauses.append("model = ?")
        values.append(model)
    if job_id is not None:
        clauses.append("job_id = ?")
        values.append(job_id)
    return (" WHERE " + " AND ".join(clauses) if clauses else "", values)


def _append_condition(where: str, condition: str) -> str:
    return f"{where} AND {condition}" if where else f" WHERE {condition}"


def _row_to_record(row: sqlite3.Row) -> LLMUsageRecord:
    return LLMUsageRecord(**{field: row[field] for field in LLMUsageRecord.__dataclass_fields__})


@dataclass
class _AttemptCounter:
    invocations: int = 0
    transport_attempts: int = 0

    @property
    def total(self) -> int:
        return max(1, self.invocations, self.transport_attempts)


_ACTIVE_COUNTER: ContextVar[_AttemptCounter | None] = ContextVar(
    "trade_compass_llm_attempt_counter",
    default=None,
)


def mark_llm_transport_attempt() -> None:
    counter = _ACTIVE_COUNTER.get()
    if counter is not None:
        counter.transport_attempts += 1


class LLMCallAudit:
    """Track one logical client call, including provider SDK retries."""

    def __init__(
        self,
        *,
        db_path: Path | None,
        provider: str,
        model: str,
        operation: str,
        timezone_name: str = "Asia/Shanghai",
    ) -> None:
        self.db_path = db_path
        self.provider = provider
        self.model = model
        self.operation = operation
        self.timezone_name = timezone_name
        self.context = current_llm_usage_context()
        self.counter = _AttemptCounter()
        self.started_at = datetime.now(timezone.utc)
        self._token: Token[_AttemptCounter | None] | None = None

    def __enter__(self) -> LLMCallAudit:
        self._token = _ACTIVE_COUNTER.set(self.counter)
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        if self._token is not None:
            _ACTIVE_COUNTER.reset(self._token)

    def mark_invocation(self) -> None:
        self.counter.invocations += 1

    def finish(
        self,
        *,
        status: str,
        usage: ProviderTokenUsage | None = None,
        model: str | None = None,
        provider_request_id: str = "",
        error: BaseException | None = None,
    ) -> None:
        if self.db_path is None:
            return
        finished_at = datetime.now(timezone.utc)
        token_usage = usage or ProviderTokenUsage()
        try:
            local_zone = ZoneInfo(self.timezone_name)
        except ZoneInfoNotFoundError:
            local_zone = timezone.utc
        record = LLMUsageRecord(
            id=str(uuid4()),
            usage_date=self.started_at.astimezone(local_zone).date().isoformat(),
            started_at=self.started_at.isoformat(),
            finished_at=finished_at.isoformat(),
            provider=self.provider,
            model=model or self.model,
            operation=self.operation,
            job_id=self.context.job_id,
            run_id=self.context.run_id,
            session_id=self.context.session_id,
            attempt=self.context.attempt,
            sdk_attempts=self.counter.total,
            status=status,
            prompt_tokens=token_usage.prompt_tokens,
            completion_tokens=token_usage.completion_tokens,
            cached_tokens=token_usage.cached_tokens,
            total_tokens=token_usage.total_tokens,
            billed_amount=token_usage.billed_amount,
            billed_currency=token_usage.billed_currency,
            cost_source="provider_reported" if token_usage.billed_amount is not None else "unavailable",
            provider_request_id=provider_request_id,
            duration_ms=max(0, round((finished_at - self.started_at).total_seconds() * 1000)),
            error_type=type(error).__name__ if error is not None else "",
        )
        try:
            SqliteLLMUsageStore(self.db_path).record(record)
        except Exception:
            logger.warning("Failed to persist LLM usage audit", exc_info=True)


def provider_token_usage(response: Any, usage: Any | None = None) -> ProviderTokenUsage:
    usage = usage if usage is not None else _value(response, "usage")
    prompt_tokens = _optional_int(_value(usage, "prompt_tokens", "input_tokens"))
    completion_tokens = _optional_int(_value(usage, "completion_tokens", "output_tokens"))
    total_tokens = _optional_int(_value(usage, "total_tokens"))
    details = _value(usage, "prompt_tokens_details", "input_tokens_details")
    cached_value = _value(details, "cached_tokens")
    if cached_value is None:
        cached_value = _value(usage, "prompt_cache_hit_tokens", "cached_tokens")
    cached_tokens = _optional_int(cached_value)
    if total_tokens is None and prompt_tokens is not None and completion_tokens is not None:
        total_tokens = prompt_tokens + completion_tokens
    billed_value = _value(usage, "billed_amount", "total_cost", "cost")
    if billed_value is None:
        billed_value = _value(response, "billed_amount", "total_cost", "cost")
    billed_amount = _optional_float(billed_value)
    billed_currency = str(
        _value(usage, "billed_currency", "cost_currency", "currency")
        or _value(response, "billed_currency", "cost_currency", "currency")
        or ""
    )
    return ProviderTokenUsage(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cached_tokens=cached_tokens,
        total_tokens=total_tokens,
        billed_amount=billed_amount,
        billed_currency=billed_currency,
    )


def _value(obj: Any, *names: str) -> Any:
    if obj is None:
        return None
    for name in names:
        if isinstance(obj, dict):
            value = obj.get(name)
            if value is not None:
                return value
            continue
        value = getattr(obj, name, None)
        if value is not None:
            return value
    return None


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
