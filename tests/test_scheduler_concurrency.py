"""Consumers remain timely while a different task is running."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import threading
from unittest.mock import Mock

from trade_compass_agent.config import AppConfig
from trade_compass_agent.ops import tick_scheduler as module
from trade_compass_agent.ops.job_executor import JobExecutor
from trade_compass_agent.ops.job_definition import JobDefinition
from trade_compass_agent.ops.run_store import SqliteRunStore
from trade_compass_agent.portfolio.trading_policy import AutonomousTradingStore
from trade_compass_agent.runtime.tools import portfolio as portfolio_tools


def test_slow_autonomous_run_preserves_other_due_jobs_watch_checks_and_restart(tmp_path, monkeypatch):
    config = AppConfig(data_dir=tmp_path/'data', memory_dir=tmp_path/'memory')
    AutonomousTradingStore(config.data_dir).set_enabled(True)
    now = [datetime(2026, 9, 14, 9, 35)]
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now[0]
    monkeypatch.setattr(module, 'datetime', Clock)
    monkeypatch.setattr(portfolio_tools, '_market_now', lambda: now[0])
    monkeypatch.setattr('trade_compass_agent.ops.trading_calendar.is_trading_day', lambda *a: True)
    monkeypatch.setattr(module, '_is_trading_day_cached', lambda: True)
    monkeypatch.setattr(module, 'sweep_scheduler_sessions', lambda *a: None)
    scheduler = module.TickScheduler(config)
    job = scheduler.prompt_store.create(name='09:40 analysis', prompt='Check current market', schedule='trading_day 09:40')
    started, release, custom_done = threading.Event(), threading.Event(), threading.Event()
    watch = Mock()
    monkeypatch.setattr(scheduler.watch_plan_monitor, 'tick', watch)
    monkeypatch.setattr(scheduler, '_reap_stale_runs', lambda: None)
    async def slow(job, trigger):
        assert job.id == 'autonomous_trading'
        started.set()
        assert release.wait(5)
    custom = Mock(side_effect=lambda *a, **k: custom_done.set())
    monkeypatch.setattr(scheduler, '_execute_and_deliver', slow)
    monkeypatch.setattr(scheduler.prompt_executor, 'execute', custom)
    try:
        scheduler._tick()
        assert started.wait(1)
        # This represents a later poll while the 13-minute trading task is still busy.
        now[0] = datetime(2026, 9, 14, 9, 40)
        scheduler._tick()
        assert custom_done.wait(1)
        assert not release.is_set() and custom.call_count == 1
        assert watch.call_args.args[0] == now[0]
        # A newly constructed instance and the same instance cannot fire this slot again.
        restarted = module.TickScheduler(config)
        monkeypatch.setattr(restarted.watch_plan_monitor, 'tick', lambda *a: None)
        monkeypatch.setattr(restarted, '_reap_stale_runs', lambda: None)
        dispatch = Mock()
        monkeypatch.setattr(restarted, '_dispatch', dispatch)
        restarted._tick()
        scheduler._tick()
        assert not dispatch.called and custom.call_count == 1
        assert restarted._last_fired[f'prompt:{job.id}'] == '2026-09-14 09:40'
    finally:
        release.set()
        assert scheduler._join_workers()


def test_independent_schedulers_atomically_claim_one_slot(tmp_path):
    config = AppConfig(data_dir=tmp_path/'data', memory_dir=tmp_path/'memory')
    schedulers = [module.TickScheduler(config) for _ in range(4)]
    with ThreadPoolExecutor(4) as pool:
        results = list(pool.map(lambda s: s._claim_slot('autonomous_trading', '2026-09-14 09:35'), schedulers))
    assert results.count(True) == 1
    restart = module.TickScheduler(config)
    assert not restart._claim_slot('autonomous_trading', '2026-09-14 09:35')
    assert not restart._claim_slot('autonomous_trading', '2026-09-14 09:05')
    assert restart._claim_slot('autonomous_trading', '2026-09-14 10:05')


def test_atomic_run_admission_covers_independent_executors(tmp_path, monkeypatch):
    import asyncio
    config = AppConfig(data_dir=tmp_path/'data', memory_dir=tmp_path/'memory')
    path = config.data_dir/'scheduler.db'
    stores = [SqliteRunStore(path) for _ in range(3)]
    job = JobDefinition(id='autonomous_trading', name='test', description='test', schedule='every 30m', workflow_id='test')
    monkeypatch.setattr('trade_compass_agent.ops.autonomous_trading.skip_reason', lambda *a: None)
    started, release = threading.Event(), threading.Event()
    def make_executor(store):
        executor = JobExecutor(config, store)
        async def execute(job, run, ctx, trigger):
            started.set()
            assert release.wait(5)
            store.complete_run(run, message='completed once')
        monkeypatch.setattr(executor, '_execute_workflow_job', execute)
        return executor
    executors = [make_executor(s) for s in stores]
    with ThreadPoolExecutor(3) as pool:
        first = pool.submit(lambda: asyncio.run(executors[0].execute(job, trigger='api')))
        try:
            assert started.wait(1)
            others = [pool.submit(lambda e=e: asyncio.run(e.execute(job, trigger='cli'))) for e in executors[1:]]
            assert [future.result(timeout=2).status for future in others] == ['skipped', 'skipped']
            assert sum(r.status == 'running' for r in stores[0].recent_runs()) == 1
        finally:
            release.set()
        assert first.result(timeout=2).status == 'completed'


def test_custom_executor_has_same_atomic_overlap_guard(tmp_path, monkeypatch):
    from trade_compass_agent.ops.prompt_jobs import PromptJobStore, PromptJobExecutor
    config = AppConfig(data_dir=tmp_path/'data', memory_dir=tmp_path/'memory')
    path = config.data_dir/'scheduler.db'
    store = SqliteRunStore(path)
    task = PromptJobStore(path).create(name='custom', prompt='analyze', schedule='every 30m')
    started, release = threading.Event(), threading.Event()
    def run(*a, **k):
        started.set()
        assert release.wait(5)
        return 'completed once'
    monkeypatch.setattr('trade_compass_agent.ops.agent_session.ScheduledAgentSession.run', run)
    monkeypatch.setattr('trade_compass_agent.ops.delivery.DeliveryRouter.deliver', lambda *a: None)
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(PromptJobExecutor(config, store).execute, task)
        try:
            assert started.wait(1)
            other = pool.submit(PromptJobExecutor(config, SqliteRunStore(path)).execute, task)
            other.result(timeout=2)
            assert sorted(r.status for r in store.recent_runs()) == ['running', 'skipped']
        finally:
            release.set()
        first.result(timeout=2)
    assert sorted(r.status for r in store.recent_runs()) == ['completed', 'skipped']
