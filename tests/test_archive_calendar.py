"""A cross-midnight recovery keeps its business day and real execution time."""
import asyncio
import json
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest

from trade_compass_agent.config import AppConfig, LLMConfig
from trade_compass_agent.memory.observation_store import ObservationStore


@pytest.mark.parametrize('zone,expected', [
    ('Asia/Shanghai', {'start', 'end', 'legacy'}),
    ('UTC', {'end', 'next', 'legacy'}),
])
def test_fixed_evidence_window_preserves_raw_timestamps(tmp_path, zone, expected):
    store = ObservationStore(tmp_path / 'observations.db')
    stamps = {'start': '2026-10-18T16:00:00+00:00', 'end': '2026-10-21T15:59:59+00:00',
              'next': '2026-10-21T16:00:00+00:00', 'legacy': '2026-10-19T00:00:00',
              'old': '2026-10-18T15:59:59+00:00'}
    for name, stamp in stamps.items():
        store.append('s', 'get_bars', name)
        store._get_conn().execute('UPDATE observations SET created_at=? WHERE summary=?', (stamp, name))
    store._get_conn().commit()
    selected = store.recent_by_days(3, as_of=date(2026, 10, 21), timezone_name=zone)
    assert {o.summary for o in selected} == expected
    assert {o.summary: o.created_at for o in store.recent(100)} == stamps


def test_job_explicit_day_reaches_workflow_without_rewriting_execution_time(tmp_path, monkeypatch):
    from trade_compass_agent.ops.job_definition import JobDefinition
    from trade_compass_agent.ops.job_executor import JobExecutor
    from trade_compass_agent.ops.run_store import SqliteRunStore
    from trade_compass_agent.runtime.workflows import engine

    seen = []
    def workflow(asset, inputs, **kwargs):
        seen.append(inputs)
        return {'run_id': kwargs['run_id']}
    monkeypatch.setattr(engine, 'run_workflow_asset_by_id', workflow)
    config = AppConfig(data_dir=tmp_path / 'data', memory_dir=tmp_path / 'memory',
                       llm=LLMConfig(provider='disabled'))
    config.data_dir.mkdir()
    config.memory_dir.mkdir()
    store = SqliteRunStore(config.data_dir / 'scheduler.db')
    job = JobDefinition('postmarket', '归档', '', '', workflow_id='postmarket_archive',
                        workflow_inputs={'date_copy': '{date}'})
    before = datetime.now().astimezone()
    day = date.today() - timedelta(days=1)
    run = asyncio.run(JobExecutor(config, store).execute(job, trigger='cli', as_of=day))
    assert run.status == 'completed'
    assert seen[0]['as_of'] == seen[0]['date_copy'] == day.isoformat()
    assert run.started_at.astimezone() >= before
    asyncio.run(JobExecutor(config, store).execute(job, trigger='cli'))
    assert seen[1]['as_of'] == date.today().isoformat()


def test_nested_reasoning_and_diary_keep_archive_day(tmp_path, monkeypatch):
    from trade_compass_agent.memory.dream_diary import (
        append_dream_diary, run_dream_diary, run_procedure_extraction,
    )
    seen = []
    class Session:
        def __init__(self, config, **kwargs):
            seen.append(kwargs)
        def run(self, prompt, **kwargs):
            return '反思'
    monkeypatch.setattr('trade_compass_agent.ops.agent_session.ScheduledAgentSession', Session)
    day = date(2026, 10, 21)
    config = AppConfig(data_dir=tmp_path / 'data', memory_dir=tmp_path,
                       llm=LLMConfig(provider='disabled'))
    run_procedure_extraction(config, [], run_date=day)
    run_dream_diary(config, '摘要', run_date=day)
    path = append_dream_diary(tmp_path, '反思', run_date=day)
    assert all(s['run_date'] == day for s in seen)
    assert '## 2026-10-21' in path.read_text()


def test_snapshots_and_previous_evidence_use_original_day(tmp_path):
    from trade_compass_agent.memory.insights import _load_previous_insights, persist_insights
    from trade_compass_agent.memory.patterns import _load_previous_patterns, persist_patterns
    day = date(2026, 10, 21)
    for kind in ['patterns', 'insights']:
        folder = tmp_path / 'dreaming' / kind
        folder.mkdir(parents=True)
        for label in ['2026-10-20', '2026-10-22']:
            (folder / (label + '.json')).write_text(json.dumps([{'theme': label, 'title': label}]))
    assert _load_previous_patterns(tmp_path, as_of=day)[0].theme == '2026-10-20'
    assert _load_previous_insights(tmp_path, as_of=day)[0]['title'] == '2026-10-20'
    assert persist_patterns(tmp_path, [], as_of=day).stem == day.isoformat()
    assert persist_insights(tmp_path, [], as_of=day).stem == day.isoformat()


def test_future_decision_results_cannot_support_past_archive():
    from trade_compass_agent.memory.insights import _dated_decisions
    from trade_compass_agent.memory.patterns import _collect_decisions
    def decision(id, decided, resolved=''):
        return SimpleNamespace(id=id, decided_at=decided, resolved_at=resolved,
            symbol='601928', side='buy', status='pending', outcome_pnl_pct=None, reasoning='')
    rows = [decision('ok', '2026-10-20T16:01:00+00:00'),
            decision('future', '2026-10-21T16:01:00+00:00'),
            decision('future-result', '2026-10-20', '2026-10-22')]
    day = date(2026, 10, 21)
    assert [d.id for d in _dated_decisions(rows, day, 'Asia/Shanghai')] == ['ok']
    assert [d['id'] for d in _collect_decisions(SimpleNamespace(search=lambda **k: rows),
            3, as_of=day)] == ['ok']
