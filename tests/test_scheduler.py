from datetime import datetime
from pathlib import Path

from trade_compass_agent.config import AppConfig, load_app_config
from trade_compass_agent.ops.job_definition import JobRegistry
from trade_compass_agent.ops.run_store import SqliteRunStore
from trade_compass_agent.ops.tick_scheduler import (
    TickScheduler,
    _parse_schedule_slot,
    _cron_field_matches,
    _DOW_NAMES,
    _MONTH_NAMES,
)


def test_registry_from_config():
    config = load_app_config()
    registry = JobRegistry()
    registry.from_config(config)
    ids = set(registry.ids())
    assert ids == {"premarket", "morning_plan", "close", "eod_review", "postmarket", "weekly"}


def test_registry_get():
    config = load_app_config()
    registry = JobRegistry()
    registry.from_config(config)
    job = registry.get("premarket")
    assert job is not None
    assert job.name == "盘前扫描"
    assert job.workflow_id == "premarket_briefing"


def test_sqlite_run_store_round_trips(tmp_path: Path):
    store = SqliteRunStore(tmp_path / "test.db")
    run = store.create_run("premarket", trigger="cli")
    assert run.status == "queued"

    store.start_run(run)
    assert run.status == "running"

    store.complete_run(run, message="ok", artifact="/tmp/test.md")
    assert run.status == "completed"
    assert run.ok

    recent = store.recent_runs(1)
    assert len(recent) == 1
    assert recent[0].id == run.id
    assert recent[0].ok


def test_sqlite_run_store_step_runs(tmp_path: Path):
    store = SqliteRunStore(tmp_path / "test.db")
    run = store.create_run("morning_plan")
    store.start_run(run)

    step = store.create_step_run(run.id, "screening")
    store.start_step(step)
    store.complete_step(step, output="Top 10 candidates")

    steps = store.step_runs_for(run.id)
    assert len(steps) == 1
    assert steps[0].step_id == "screening"
    assert steps[0].status == "completed"


def test_sqlite_run_store_fail(tmp_path: Path):
    store = SqliteRunStore(tmp_path / "test.db")
    run = store.create_run("close")
    store.start_run(run)
    store.fail_run(run, error="API timeout")
    assert run.status == "failed"
    assert not run.ok

    fetched = store.get_run(run.id)
    assert fetched is not None
    assert fetched.error == "API timeout"


def test_sqlite_run_store_degraded(tmp_path: Path):
    store = SqliteRunStore(tmp_path / "test.db")
    run = store.create_run("morning_plan")
    store.start_run(run)
    store.degrade_run(
        run,
        error="Agent 执行失败: read timed out",
        message="morning_plan: agent_plan failed/degraded",
        artifact="/tmp/morning-plan.jsonl",
    )

    assert run.status == "degraded"
    assert not run.ok
    fetched = store.get_run(run.id)
    assert fetched is not None
    assert fetched.status == "degraded"
    assert fetched.error == "Agent 执行失败: read timed out"
    assert fetched.artifact == "/tmp/morning-plan.jsonl"


def test_sqlite_run_store_skip(tmp_path: Path):
    store = SqliteRunStore(tmp_path / "test.db")
    run = store.create_run("premarket")
    store.skip_run(run, reason="非交易日")
    assert run.status == "skipped"


def test_sqlite_import_from_jsonl(tmp_path: Path):
    jsonl = tmp_path / "job_runs.jsonl"
    jsonl.write_text(
        '{"id":"run-1","job_id":"close","started_at":"2025-01-01T15:10:00","finished_at":"2025-01-01T15:10:05","ok":true,"message":"ok"}\n'
        '{"id":"run-2","job_id":"postmarket","started_at":"2025-01-01T16:30:00","finished_at":"2025-01-01T16:30:10","ok":false,"message":"fail","error":"timeout"}\n',
        encoding="utf-8",
    )
    store = SqliteRunStore(tmp_path / "test.db")
    count = store.import_from_jsonl(jsonl)
    assert count == 2

    runs = store.recent_runs(10)
    assert len(runs) == 2
    ids = {r.id for r in runs}
    assert "run-1" in ids
    assert "run-2" in ids


def test_tick_scheduler_builds_without_starting():
    scheduler = TickScheduler(load_app_config())
    jobs = scheduler.list_jobs()
    assert len(jobs) == 6
    assert {j.id for j in jobs} >= {"close", "postmarket", "weekly"}
    assert not scheduler.running


def test_scheduler_running_requires_live_thread(tmp_path: Path):
    config = AppConfig(data_dir=tmp_path / "data", memory_dir=tmp_path / "memory")
    scheduler = TickScheduler(config)
    scheduler._running = True
    scheduler._thread = None

    assert scheduler.running is False


def test_builtin_job_timeouts_cover_workflow_timeouts():
    registry = JobRegistry()
    registry.from_config(load_app_config())

    assert registry.get("premarket").timeout_seconds >= 1200
    assert registry.get("close").timeout_seconds >= 900
    assert registry.get("eod_review").timeout_seconds >= 1200


def test_tick_scheduler_can_skip_startup_reaper_for_transient_instances(tmp_path: Path):
    config = AppConfig(data_dir=tmp_path / "data", memory_dir=tmp_path / "memory")
    store = SqliteRunStore(config.data_dir / "scheduler.db")
    run = store.create_run("morning_plan")
    store.start_run(run)
    step = store.create_step_run(run.id, "workflow")
    store.start_step(step)

    scheduler = TickScheduler(config, reap_on_init=False)

    assert not scheduler.running
    assert scheduler.run_store.is_job_running("morning_plan")
    steps = scheduler.run_store.step_runs_for(run.id)
    assert steps[0].status == "running"


def test_overlap_guard(tmp_path: Path):
    store = SqliteRunStore(tmp_path / "test.db")
    run = store.create_run("premarket")
    store.start_run(run)
    assert store.is_job_running("premarket")
    assert not store.is_job_running("close")
    store.complete_run(run, message="done")
    assert not store.is_job_running("premarket")


def test_reap_stale_runs(tmp_path: Path):
    from datetime import datetime, timedelta

    store = SqliteRunStore(tmp_path / "test.db")
    run = store.create_run("premarket")
    store.start_run(run)
    step = store.create_step_run(run.id, "agent_briefing")
    store.start_step(step)

    stale_started = datetime.now() - timedelta(hours=2)
    import sqlite3
    with sqlite3.connect(tmp_path / "test.db") as conn:
        conn.execute(
            "UPDATE job_runs SET started_at = ? WHERE id = ?",
            (stale_started.isoformat(), run.id),
        )
        conn.execute(
            "UPDATE step_runs SET started_at = ? WHERE id = ?",
            (stale_started.isoformat(), step.id),
        )

    reaped = store.reap_stale_runs({"premarket": 600}, grace_seconds=300)
    assert reaped == [run.id]
    assert not store.is_job_running("premarket")
    fetched = store.get_run(run.id)
    assert fetched is not None
    assert fetched.status == "failed"
    steps = store.step_runs_for(run.id)
    assert steps[0].status == "failed"


def test_reap_stale_runs_keeps_recent(tmp_path: Path):
    store = SqliteRunStore(tmp_path / "test.db")
    run = store.create_run("morning_plan")
    store.start_run(run)
    reaped = store.reap_stale_runs({"morning_plan": 1800}, grace_seconds=300)
    assert reaped == []
    assert store.is_job_running("morning_plan")


def test_reap_interrupted_runs_on_startup(tmp_path: Path):
    store = SqliteRunStore(tmp_path / "test.db")
    run = store.create_run("morning_plan")
    store.start_run(run)
    step = store.create_step_run(run.id, "agent_plan")
    store.start_step(step)

    reaped = store.reap_interrupted_runs()
    assert reaped == [run.id]
    assert not store.is_job_running("morning_plan")
    fetched = store.get_run(run.id)
    assert fetched is not None
    assert fetched.status == "failed"
    assert "process restarted" in (fetched.error or "")


def test_reap_orphan_step_runs_when_parent_is_terminal(tmp_path: Path):
    store = SqliteRunStore(tmp_path / "test.db")
    run = store.create_run("premarket")
    store.start_run(run)
    step = store.create_step_run(run.id, "agent_briefing")
    store.start_step(step)
    store.fail_run(run, error="agent failed")

    count = store.reap_orphan_step_runs()

    assert count == 1
    steps = store.step_runs_for(run.id)
    assert steps[0].status == "failed"
    assert steps[0].finished_at is not None
    assert "parent run is not running" in (steps[0].error or "")


def test_reap_orphan_step_runs_keeps_active_parent(tmp_path: Path):
    store = SqliteRunStore(tmp_path / "test.db")
    run = store.create_run("premarket")
    store.start_run(run)
    step = store.create_step_run(run.id, "agent_briefing")
    store.start_step(step)

    count = store.reap_orphan_step_runs()

    assert count == 0
    steps = store.step_runs_for(run.id)
    assert steps[0].status == "running"


# ---------------------------------------------------------------------------
# Cron expression tests
# ---------------------------------------------------------------------------


class TestCronFieldMatches:
    def test_wildcard(self):
        assert _cron_field_matches("*", 5, 0, 59) is True

    def test_single_value(self):
        assert _cron_field_matches("30", 30, 0, 59) is True
        assert _cron_field_matches("30", 15, 0, 59) is False

    def test_comma_list(self):
        assert _cron_field_matches("0,15,30,45", 15, 0, 59) is True
        assert _cron_field_matches("0,15,30,45", 10, 0, 59) is False

    def test_range(self):
        assert _cron_field_matches("1-5", 3, 0, 6) is True
        assert _cron_field_matches("1-5", 0, 0, 6) is False
        assert _cron_field_matches("1-5", 6, 0, 6) is False

    def test_step_wildcard(self):
        assert _cron_field_matches("*/15", 0, 0, 59) is True
        assert _cron_field_matches("*/15", 15, 0, 59) is True
        assert _cron_field_matches("*/15", 30, 0, 59) is True
        assert _cron_field_matches("*/15", 7, 0, 59) is False

    def test_step_range(self):
        assert _cron_field_matches("10-20/5", 10, 0, 59) is True
        assert _cron_field_matches("10-20/5", 15, 0, 59) is True
        assert _cron_field_matches("10-20/5", 12, 0, 59) is False
        assert _cron_field_matches("10-20/5", 25, 0, 59) is False

    def test_named_days(self):
        # POSIX: mon=1
        assert _cron_field_matches("mon", 1, 0, 7, _DOW_NAMES) is True
        assert _cron_field_matches("mon", 2, 0, 7, _DOW_NAMES) is False
        assert _cron_field_matches("mon-fri", 3, 0, 7, _DOW_NAMES) is True
        assert _cron_field_matches("mon-fri", 0, 0, 7, _DOW_NAMES) is False  # Sun

    def test_named_months(self):
        assert _cron_field_matches("jan", 1, 1, 12, _MONTH_NAMES) is True
        assert _cron_field_matches("jun-aug", 7, 1, 12, _MONTH_NAMES) is True
        assert _cron_field_matches("jun-aug", 5, 1, 12, _MONTH_NAMES) is False


class TestCronScheduleSlot:
    def test_cron_every_minute(self):
        now = datetime(2025, 6, 15, 9, 30, 10)
        slot = _parse_schedule_slot("cron * * * * *", now)
        assert slot == datetime(2025, 6, 15, 9, 30, 0)

    def test_cron_specific_time(self):
        now = datetime(2025, 6, 15, 9, 30, 10)
        slot = _parse_schedule_slot("cron 30 9 * * *", now)
        assert slot == datetime(2025, 6, 15, 9, 30, 0)

    def test_cron_no_match_minute(self):
        now = datetime(2025, 6, 15, 9, 31, 0)
        slot = _parse_schedule_slot("cron 30 9 * * *", now)
        assert slot is None

    def test_cron_posix_dow_sunday_is_0(self):
        """POSIX cron: 0=Sunday. 2025-06-15 is a Sunday."""
        now = datetime(2025, 6, 15, 10, 0, 0)
        slot = _parse_schedule_slot("cron 0 10 * * 0", now)
        assert slot is not None

    def test_cron_posix_dow_monday_is_1(self):
        """POSIX cron: 1=Monday. 2025-06-16 is a Monday."""
        now = datetime(2025, 6, 16, 10, 0, 0)
        slot = _parse_schedule_slot("cron 0 10 * * 1", now)
        assert slot is not None

    def test_cron_posix_dow_no_match(self):
        """2025-06-17 is Tuesday (POSIX 2), cron asks for Monday (1)."""
        now = datetime(2025, 6, 17, 10, 0, 0)
        slot = _parse_schedule_slot("cron 0 10 * * 1", now)
        assert slot is None

    def test_cron_named_day(self):
        """Named weekday: MON. 2025-06-16 is Monday."""
        now = datetime(2025, 6, 16, 9, 0, 0)
        slot = _parse_schedule_slot("cron 0 9 * * MON", now)
        assert slot is not None

    def test_cron_named_day_range(self):
        """MON-FRI range. 2025-06-18 is Wednesday."""
        now = datetime(2025, 6, 18, 9, 30, 0)
        slot = _parse_schedule_slot("cron 30 9 * * MON-FRI", now)
        assert slot is not None

    def test_cron_named_day_range_no_match(self):
        """MON-FRI range. 2025-06-15 is Sunday — should not match."""
        now = datetime(2025, 6, 15, 9, 30, 0)
        slot = _parse_schedule_slot("cron 30 9 * * MON-FRI", now)
        assert slot is None

    def test_cron_every_15_min(self):
        now = datetime(2025, 6, 15, 14, 45, 5)
        slot = _parse_schedule_slot("cron */15 * * * *", now)
        assert slot == datetime(2025, 6, 15, 14, 45, 0)

    def test_cron_invalid_fields(self):
        now = datetime(2025, 6, 15, 9, 30, 0)
        slot = _parse_schedule_slot("cron * *", now)
        assert slot is None

    def test_shortcut_daily(self):
        now = datetime(2025, 6, 15, 0, 0, 30)
        slot = _parse_schedule_slot("@daily", now)
        assert slot == datetime(2025, 6, 15, 0, 0, 0)

    def test_shortcut_daily_no_match(self):
        now = datetime(2025, 6, 15, 0, 1, 0)
        slot = _parse_schedule_slot("@daily", now)
        assert slot is None

    def test_shortcut_hourly(self):
        now = datetime(2025, 6, 15, 14, 0, 5)
        slot = _parse_schedule_slot("@hourly", now)
        assert slot == datetime(2025, 6, 15, 14, 0, 0)

    def test_shortcut_weekly(self):
        """@weekly = Sunday 00:00. 2025-06-15 is Sunday."""
        now = datetime(2025, 6, 15, 0, 0, 0)
        slot = _parse_schedule_slot("@weekly", now)
        assert slot is not None

    def test_shortcut_monthly(self):
        now = datetime(2025, 6, 1, 0, 0, 0)
        slot = _parse_schedule_slot("@monthly", now)
        assert slot is not None

    def test_cron_named_month(self):
        """Match June via JUN name."""
        now = datetime(2025, 6, 15, 9, 30, 0)
        slot = _parse_schedule_slot("cron 30 9 * JUN *", now)
        assert slot is not None

    def test_cron_named_month_no_match(self):
        """JAN should not match June."""
        now = datetime(2025, 6, 15, 9, 30, 0)
        slot = _parse_schedule_slot("cron 30 9 * JAN *", now)
        assert slot is None


def test_agent_daily_journal_injects_reflection_context() -> None:
    import asyncio
    from datetime import date
    from unittest.mock import patch

    from trade_compass_agent.config import AppConfig
    from trade_compass_agent.ops.job_definition import StepContext, StepOutput
    from trade_compass_agent.runtime.tools.builtin_operations import agent_daily_journal

    ctx = StepContext(
        config=AppConfig(),
        date=date(2026, 6, 15),
        reflection_context="跨 run lesson A",
    )
    ctx.upstream["audit_summary"] = StepOutput(message="audit", data={"agent_turns": 3})
    ctx.upstream["reflection"] = StepOutput(
        message="resolved",
        data={"lessons": ["今日 lesson"]},
    )
    ctx.upstream["memory_compact"] = StepOutput(message="ok")

    captured: dict[str, str] = {}

    async def capture_run_agent_step(_ctx, prompt, _job_id, *, step_id=None):
        captured["prompt"] = prompt
        return StepOutput(message="done", data={})

    with patch("trade_compass_agent.runtime.tools.builtin_operations.run_agent_step", capture_run_agent_step):
        asyncio.run(agent_daily_journal(ctx))

    assert "跨 run lesson A" in captured["prompt"]
    assert "今日 lesson" in captured["prompt"]
