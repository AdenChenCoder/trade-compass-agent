"""TickScheduler — polling-based scheduler replacing APScheduler.

Design principles:
- At-most-once: each job fires at most once per scheduled slot
- Grace window: missed jobs within 5 minutes are still eligible
- Overlap guard: same job can't run concurrently (checked via RunStore)
- Trading day awareness: "trading_day HH:MM" skips non-trading days
"""

from __future__ import annotations

import asyncio
import logging
import re
import threading
import time
from datetime import datetime

from trade_compass_agent.config import AppConfig, load_app_config
from trade_compass_agent.ops.delivery import DeliveryRouter
from trade_compass_agent.ops.hooks import create_default_registry
from trade_compass_agent.ops.job_definition import JobDefinition, JobRegistry
from trade_compass_agent.ops.job_executor import JobExecutor
from trade_compass_agent.ops.prompt_jobs import PromptJob, PromptJobExecutor, PromptJobStore
from trade_compass_agent.ops.run_store import SqliteRunStore
from trade_compass_agent.ops.session_cleanup import sweep_scheduler_sessions
from trade_compass_agent.ops.watch_plans import WatchPlanMonitor, WatchPlanStore

logger = logging.getLogger(__name__)

TICK_INTERVAL_SECONDS = 30
GRACE_WINDOW_SECONDS = 300  # 5 minutes


class TickScheduler:
    """Polling scheduler that checks every TICK_INTERVAL_SECONDS."""

    def __init__(self, config: AppConfig | None = None, *, reap_on_init: bool = False) -> None:
        self.config = config or load_app_config()
        db_path = self.config.data_dir / "scheduler.db"
        self.run_store = SqliteRunStore(db_path)
        self.registry = JobRegistry()
        self.registry.from_config(self.config)
        self.hook_registry = create_default_registry()
        self.executor = JobExecutor(self.config, self.run_store, hook_registry=self.hook_registry)
        self.delivery = DeliveryRouter(self.config)
        self.prompt_store = PromptJobStore(db_path)
        self.prompt_executor = PromptJobExecutor(self.config, self.run_store)
        self.watch_plan_store = WatchPlanStore(self.config.data_dir / "watch_plans.json")
        self.watch_plan_monitor = WatchPlanMonitor(self.config, store=self.watch_plan_store)

        self._running = False
        self._thread: threading.Thread | None = None
        self._db_path = db_path
        self._init_state_table()
        self._last_fired = self._load_last_fired()
        self._migrate_legacy_jsonl()
        if reap_on_init:
            self.run_store.reap_interrupted_runs()
            self.run_store.reap_orphan_step_runs()

    def _init_state_table(self) -> None:
        import sqlite3
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS scheduler_state "
                "(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )

    def _load_last_fired(self) -> dict[str, str]:
        import sqlite3
        result: dict[str, str] = {}
        with sqlite3.connect(self._db_path) as conn:
            for row in conn.execute(
                "SELECT key, value FROM scheduler_state WHERE key LIKE 'last_fired:%'"
            ):
                job_id = row[0].removeprefix("last_fired:")
                result[job_id] = row[1]
        return result

    def _save_last_fired(self, job_id: str, slot_key: str) -> None:
        import sqlite3
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO scheduler_state (key, value) VALUES (?, ?)",
                (f"last_fired:{job_id}", slot_key),
            )

    def _migrate_legacy_jsonl(self) -> None:
        """One-time import from legacy JSONL if it exists."""
        jsonl_path = self.config.data_dir / "job_runs.jsonl"
        if jsonl_path.exists():
            count = self.run_store.import_from_jsonl(jsonl_path)
            if count > 0:
                backup = jsonl_path.with_suffix(".jsonl.bak")
                jsonl_path.rename(backup)
                logger.info("Migrated %d legacy runs; original moved to %s", count, backup)

    @property
    def running(self) -> bool:
        return bool(self._running and self._thread and self._thread.is_alive())

    def start_background(self) -> None:
        if not self.config.scheduler.enabled:
            logger.info("Scheduler disabled in config")
            return
        if self.running:
            return
        if self._running:
            logger.warning("TickScheduler thread stopped unexpectedly; restarting")
        self._running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="tick-scheduler")
        self._thread.start()
        logger.info("TickScheduler started (tick=%ds)", TICK_INTERVAL_SECONDS)

    def shutdown(self, *, wait: bool = True) -> None:
        self._running = False
        if self._thread and wait:
            self._thread.join(timeout=10)
        self._thread = None
        logger.info("TickScheduler stopped")

    def start_blocking(self) -> None:
        self.start_background()
        try:
            while self._running:
                time.sleep(1)
        except KeyboardInterrupt:
            self.shutdown(wait=False)

    def reload(self) -> None:
        """Reload config and re-register jobs."""
        self.config = load_app_config()
        self.registry = JobRegistry()
        self.registry.from_config(self.config)
        self.hook_registry = create_default_registry()
        self.executor = JobExecutor(self.config, self.run_store, hook_registry=self.hook_registry)
        self.delivery = DeliveryRouter(self.config)
        self.prompt_executor = PromptJobExecutor(self.config, self.run_store)
        self.watch_plan_store = WatchPlanStore(self.config.data_dir / "watch_plans.json")
        self.watch_plan_monitor = WatchPlanMonitor(self.config, store=self.watch_plan_store)
        logger.info("TickScheduler reloaded")

    def list_jobs(self) -> list[JobDefinition]:
        return self.registry.all()

    def run_job_now(self, job_id: str, trigger: str = "api") -> None:
        """Manually trigger a job; api/cli triggers bypass schedule and trading-day gates."""
        job = self.registry.get(job_id)
        if not job:
            raise ValueError(f"Unknown job: {job_id}")
        asyncio.run(self._execute_and_deliver(job, trigger=trigger))

    # -- Internal loop --

    def _run_loop(self) -> None:
        while self._running:
            try:
                self._tick()
            except Exception:
                logger.exception("Tick error")
            time.sleep(TICK_INTERVAL_SECONDS)

    def _reap_stale_runs(self) -> None:
        timeouts = {job.id: job.timeout_seconds for job in self.registry.all()}
        self.run_store.reap_stale_runs(timeouts)
        self.run_store.reap_orphan_step_runs()

    def _tick(self) -> None:
        now = datetime.now()
        self._reap_stale_runs()
        try:
            sweep_scheduler_sessions(self.config.data_dir)
        except Exception:
            logger.exception("Scheduler session cleanup failed")
        try:
            self.watch_plan_monitor.tick(now)
        except Exception:
            logger.exception("Watch-plan monitor tick failed")
        # Built-in jobs
        for job in self.registry.all():
            if not job.enabled:
                continue
            slot = _compute_slot(job, now)
            if slot is None:
                continue
            slot_key = slot.strftime("%Y-%m-%d %H:%M")
            if self._last_fired.get(job.id) == slot_key:
                continue
            if (now - slot).total_seconds() > GRACE_WINDOW_SECONDS:
                continue
            self._last_fired[job.id] = slot_key
            self._save_last_fired(job.id, slot_key)
            logger.info("Firing job %s for slot %s", job.id, slot_key)
            try:
                asyncio.run(self._execute_and_deliver(job, trigger="scheduler"))
            except Exception:
                logger.exception("Failed to execute job %s", job.id)

        # User-created prompt jobs
        for pjob in self.prompt_store.list_enabled():
            slot = _compute_prompt_slot(pjob, now)
            if slot is None:
                continue
            slot_key = slot.strftime("%Y-%m-%d %H:%M")
            pjob_key = f"prompt:{pjob.id}"
            if self._last_fired.get(pjob_key) == slot_key:
                continue
            if (now - slot).total_seconds() > GRACE_WINDOW_SECONDS:
                continue
            if pjob.trading_day_only and not _is_trading_day_cached():
                continue
            self._last_fired[pjob_key] = slot_key
            self._save_last_fired(pjob_key, slot_key)
            logger.info("Firing prompt job %s (%s) for slot %s", pjob.name, pjob.id, slot_key)
            try:
                self.prompt_executor.execute(pjob, trigger="scheduler")
            except Exception:
                logger.exception("Failed to execute prompt job %s", pjob.id)

    async def _execute_and_deliver(self, job: JobDefinition, trigger: str = "scheduler") -> None:
        run = await self.executor.execute(job, trigger=trigger)
        self.delivery.deliver(run, job.delivery)


# ---------------------------------------------------------------------------
# Schedule parsing
# ---------------------------------------------------------------------------

_WEEKDAY_MAP = {
    "mon": 0, "tue": 1, "wed": 2, "thu": 3,
    "fri": 4, "sat": 5, "sun": 6,
}


def _parse_schedule_slot(schedule: str, now: datetime) -> datetime | None:
    """Unified schedule parser. Returns the most recent eligible fire slot, or None.

    Supported formats:
    - "trading_day HH:MM"     — daily at HH:MM on trading days
    - "sat HH:MM" / "mon HH:MM" — weekly on a specific weekday
    - "every 2h" / "every 30m" — interval-based
    - "once YYYY-MM-DDTHH:MM" — one-shot at a specific datetime
    - "cron M H D MON DOW"    — standard 5-field cron (minute hour day month weekday)
    """
    schedule = schedule.strip()

    # "trading_day HH:MM"
    m = re.match(r"trading_day\s+(\d{1,2}:\d{2})", schedule)
    if m:
        h, mi = map(int, m.group(1).split(":"))
        slot = now.replace(hour=h, minute=mi, second=0, microsecond=0)
        return slot if slot <= now else None

    # "cron M H D MON DOW" — 5-field cron expression
    m = re.match(r"cron\s+(.+)$", schedule)
    if m:
        return _match_cron(m.group(1).strip(), now)

    # @shortcuts: @yearly, @monthly, @weekly, @daily, @hourly
    shortcut = _CRON_SHORTCUTS.get(schedule.lower())
    if shortcut is not None:
        return _match_cron(shortcut, now)

    # "sat 10:00" / "mon 09:00" etc.
    m = re.match(r"(\w{3})\s+(\d{1,2}:\d{2})", schedule)
    if m:
        day_str, hhmm = m.group(1), m.group(2)
        target_weekday = _WEEKDAY_MAP.get(day_str.lower())
        if target_weekday is None:
            logger.warning("Unknown weekday: %s", day_str)
            return None
        if now.weekday() != target_weekday:
            return None
        h, mi = map(int, hhmm.split(":"))
        slot = now.replace(hour=h, minute=mi, second=0, microsecond=0)
        return slot if slot <= now else None

    # "every 2h" / "every 30m" / "every 4h30m"
    m = re.match(r"every\s+(?:(\d+)h)?(?:(\d+)m)?$", schedule)
    if m and (m.group(1) or m.group(2)):
        hours = int(m.group(1) or 0)
        minutes = int(m.group(2) or 0)
        from datetime import timedelta
        interval = timedelta(hours=hours, minutes=minutes)
        if interval.total_seconds() <= 0:
            return None
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        slot = midnight
        best = None
        while slot <= now:
            best = slot
            slot += interval
        return best

    # "once YYYY-MM-DDTHH:MM"
    m = re.match(r"once\s+(\d{4}-\d{2}-\d{2}T\d{2}:\d{2})", schedule)
    if m:
        target = datetime.fromisoformat(m.group(1))
        if target <= now:
            return target
        return None

    logger.warning("Unsupported schedule format: %s", schedule)
    return None


# ---------------------------------------------------------------------------
# Cron helpers (no external dependency, POSIX-compatible)
# ---------------------------------------------------------------------------

_CRON_SHORTCUTS: dict[str, str] = {
    "@yearly": "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
    "@monthly": "0 0 1 * *",
    "@weekly": "0 0 * * 0",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@hourly": "0 * * * *",
}

_DOW_NAMES: dict[str, int] = {
    "sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6,
}

_MONTH_NAMES: dict[str, int] = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def _match_cron(expr: str, now: datetime) -> datetime | None:
    """Match a 5-field cron expression against `now`. Returns the slot if matched.

    Fields: minute hour day-of-month month day-of-week
    Day-of-week follows POSIX: 0=Sun, 1=Mon ... 6=Sat (7=Sun also accepted).
    Supports named days (MON-SUN) and months (JAN-DEC).
    """
    fields = expr.split()
    if len(fields) != 5:
        logger.warning("Invalid cron expression (need 5 fields): %s", expr)
        return None

    minute_f, hour_f, dom_f, month_f, dow_f = fields

    # Convert Python weekday (0=Mon..6=Sun) to POSIX (0=Sun..6=Sat)
    posix_dow = (now.weekday() + 1) % 7

    if not _cron_field_matches(month_f, now.month, 1, 12, _MONTH_NAMES):
        return None
    if not _cron_field_matches(dom_f, now.day, 1, 31):
        return None
    if not _cron_field_matches(dow_f, posix_dow, 0, 7, _DOW_NAMES):
        return None
    if not _cron_field_matches(hour_f, now.hour, 0, 23):
        return None
    if not _cron_field_matches(minute_f, now.minute, 0, 59):
        return None

    return now.replace(second=0, microsecond=0)


def _cron_field_matches(
    field: str,
    value: int,
    lo: int,
    hi: int,
    name_map: dict[str, int] | None = None,
) -> bool:
    """Check if `value` matches a single cron field.

    Supports: *, single value, comma list, range (1-5), step (*/15, 10-20/5),
    and named values via name_map (e.g. MON, JAN).
    """
    field = field.lower()

    for part in field.split(","):
        part = part.strip()
        if part == "*":
            return True

        # Resolve names before numeric parsing
        part = _resolve_names(part, name_map)

        # */N  or  range/N
        if "/" in part:
            base, step_s = part.split("/", 1)
            step = int(step_s)
            if step <= 0:
                continue
            if base == "*":
                if (value - lo) % step == 0:
                    return True
            elif "-" in base:
                r_lo, r_hi = map(int, base.split("-", 1))
                if r_lo <= value <= r_hi and (value - r_lo) % step == 0:
                    return True
            continue

        # range: e.g. 1-5
        if "-" in part:
            r_lo, r_hi = map(int, part.split("-", 1))
            if r_lo <= value <= r_hi:
                return True
            continue

        # single value
        if int(part) == value:
            return True

    return False


def _resolve_names(token: str, name_map: dict[str, int] | None) -> str:
    """Replace named tokens (e.g. 'mon', 'jan') with their numeric equivalents."""
    if name_map is None:
        return token
    for name, num in name_map.items():
        token = token.replace(name, str(num))
    return token


def _compute_slot(job: JobDefinition, now: datetime) -> datetime | None:
    return _parse_schedule_slot(job.schedule, now)


def _compute_prompt_slot(pjob: PromptJob, now: datetime) -> datetime | None:
    return _parse_schedule_slot(pjob.schedule, now)


_trading_day_cache: dict[str, bool] = {}


def _is_trading_day_cached() -> bool:
    today = datetime.now().strftime("%Y-%m-%d")
    if today not in _trading_day_cache:
        from trade_compass_agent.ops.trading_calendar import is_trading_day
        _trading_day_cache[today] = is_trading_day()
    return _trading_day_cache[today]


# ---------------------------------------------------------------------------
# Module-level active scheduler (same pattern as old scheduler.py)
# ---------------------------------------------------------------------------

_active_scheduler: TickScheduler | None = None


def set_active_scheduler(scheduler: TickScheduler | None) -> None:
    global _active_scheduler
    _active_scheduler = scheduler


def get_active_scheduler() -> TickScheduler | None:
    return _active_scheduler


def reload_active_scheduler() -> bool:
    active = get_active_scheduler()
    if active is None:
        return False
    active.reload()
    return True
