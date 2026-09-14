from datetime import datetime, timedelta
import asyncio

import pytest

from trade_compass_agent.config import AppConfig
from trade_compass_agent.ops.autonomous_trading import RUN_TIMES, SCHEDULE
from trade_compass_agent.ops.job_definition import JobRegistry
from trade_compass_agent.ops.job_executor import JobExecutor
from trade_compass_agent.ops.run_store import SqliteRunStore
from trade_compass_agent.ops.tick_scheduler import _parse_schedule_slot, GRACE_WINDOW_SECONDS
from trade_compass_agent.portfolio.trading_policy import AutonomousTradingStore
from trade_compass_agent.runtime.tools import portfolio


def test_intraday_has_eight_slots_and_restart_grace():
    midnight = datetime(2026, 9, 10)
    fired = set()
    for minute in range(24 * 60):
        now = midnight + timedelta(minutes=minute)
        slot = _parse_schedule_slot(SCHEDULE, now)
        if slot and (now-slot).total_seconds() <= GRACE_WINDOW_SECONDS:
            fired.add((slot.hour, slot.minute))
    assert fired == set(RUN_TIMES)
    assert len(fired) == 8
    assert _parse_schedule_slot(SCHEDULE, midnight.replace(hour=10, minute=8)).minute == 5


@pytest.mark.parametrize('enabled,now,trading_day', [
    (False, datetime(2026, 9, 10, 10, 5), True),
    (True, datetime(2026, 9, 10, 12), True),
    (True, datetime(2026, 9, 10, 16), True),
    (True, datetime(2026, 10, 1, 10, 5), False),
])
def test_intraday_api_and_scheduler_skip_without_llm(tmp_path, monkeypatch, enabled, now, trading_day):
    config = AppConfig(data_dir=tmp_path/'data', memory_dir=tmp_path/'memory')
    AutonomousTradingStore(config.data_dir).set_enabled(enabled)
    monkeypatch.setattr(portfolio, '_market_now', lambda: now)
    monkeypatch.setattr('trade_compass_agent.ops.trading_calendar.is_trading_day', lambda day=None: trading_day)
    monkeypatch.setattr('trade_compass_agent.runtime.workflows.engine.run_workflow_asset_by_id',
                        lambda *a, **kw: pytest.fail('Inactive workflow must not start'))
    registry = JobRegistry()
    registry.from_config(config)
    executor = JobExecutor(config, SqliteRunStore(config.data_dir/'scheduler.db'))
    for trigger in ('api', 'scheduler'):
        run = asyncio.run(executor.execute(registry.get('autonomous_trading'), trigger=trigger))
        assert run.status == 'skipped'
        assert run.message
