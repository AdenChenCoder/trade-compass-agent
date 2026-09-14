from dataclasses import replace
import json
import secrets
import time
from types import SimpleNamespace

import pytest

from trade_compass_agent.config import AppConfig
from trade_compass_agent.domain import Notification
from trade_compass_agent.mobile.pairing import DeviceStore
from trade_compass_agent.mobile.push import PushStore
from trade_compass_agent.ops.delivery import DeliveryRouter
from trade_compass_agent.ops.job_definition import DeliveryConfig
from trade_compass_agent.ops.notifications import JsonNotificationStore
from trade_compass_agent.ops.run_store import SqliteRunStore

from test_mobile_pwa import subscription


@pytest.fixture
def setup(tmp_path, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(time, 'time', lambda: clock[0])
    config = AppConfig(data_dir=tmp_path / 'data', memory_dir=tmp_path / 'memory')
    devices = DeviceStore(config.data_dir / 'mobile')
    push = PushStore(config.data_dir / 'mobile', 'https://computer.example')
    secret = secrets.token_urlsafe(32)
    device = devices.claim(devices.create_invitation()['invitation'], 'phone', secret)
    devices.approve(device['device_id'], device['verification_code'])
    push.subscribe(device['device_id'], subscription()[2])
    source = JsonNotificationStore(config.data_dir / 'notifications.jsonl')
    runs = SqliteRunStore(config.data_dir / 'scheduler.db')
    calls = []
    monkeypatch.setattr(PushStore, 'send_payload', lambda self, sub, payload, **kwargs:
                        calls.append((json.loads(payload), kwargs)) or 'accepted')
    return SimpleNamespace(config=config, devices=devices, push=push, device=device['device_id'],
        clock=clock, source=source, runs=runs, calls=calls)


def deliver(f, *, policy=DeliveryConfig(), config=None, failed=False):
    run = f.runs.create_run('private-job-name')
    f.runs.start_run(run)
    if failed:
        f.runs.fail_run(run, error='private failure')
    else:
        f.runs.complete_run(run, message='private research and original result')
    DeliveryRouter(config or f.config).deliver(run, policy)
    return run


def reconcile(f):
    f.push.tasks.reconcile(f.source.events(), f.devices)


def test_opt_in_delivery_same_original_result_privacy_and_restart_dedup(setup):
    f = setup
    old = deliver(f)
    reconcile(f)
    assert not f.push.tasks.dispatch_one(f.devices)
    f.clock[0] += 1
    f.push.tasks.set_enabled(f.device, True)
    DeliveryRouter(f.config).deliver(old, DeliveryConfig())
    reconcile(f)
    assert not f.push.tasks.dispatch_one(f.devices)
    f.clock[0] += 1
    run = deliver(f)
    assert [n.message for n in f.source.recent()] == ['private research and original result'] * 3
    reconcile(f)
    assert f.push.tasks.dispatch_one(f.devices)
    payload, headers = f.calls[0]
    assert payload['kind'] == 'task'
    assert headers['topic'] == payload['id']
    assert 'private' not in json.dumps(payload) and old.id not in json.dumps(payload)
    f.push = PushStore(f.config.data_dir / 'mobile', 'https://computer.example')
    assert f.push.tasks.status(f.device)['tasks_enabled']
    # Repeated delivery uses the run identity. Older records retain their event metadata.
    DeliveryRouter(f.config).deliver(run, DeliveryConfig())
    reconcile(f)
    assert not f.push.tasks.dispatch_one(f.devices)
    assert len(f.calls) == 1
    assert f.push.tasks.received('different-device', payload['id']) == 0
    assert f.push.tasks.received(f.device, payload['id']) == 1
    assert f.push.tasks.status(f.device)['last_task']['received_at'] == f.clock[0]


@pytest.mark.parametrize('policy,notifications_enabled,failed,expected', [
    (DeliveryConfig(silent_on_success=True), True, False, False),
    (DeliveryConfig(silent_on_success=True), True, True, True),
    (DeliveryConfig(channels=()), True, False, False),
    (DeliveryConfig(), False, False, False),
])
def test_existing_job_delivery_policy_is_preserved(setup, policy, notifications_enabled, failed, expected):
    f = setup
    f.push.tasks.set_enabled(f.device, True)
    cfg = replace(f.config, notifications=replace(f.config.notifications, enabled=notifications_enabled))
    deliver(f, policy=policy, config=cfg, failed=failed)
    reconcile(f)
    assert f.push.tasks.dispatch_one(f.devices) is expected


def test_retry_lease_and_restart_keep_same_delivery_id_without_reexecuting_job(setup, monkeypatch):
    f = setup
    f.push.tasks.set_enabled(f.device, True)
    run = deliver(f)
    reconcile(f)
    original = PushStore.send_payload
    monkeypatch.setattr(PushStore, 'send_payload', lambda self, sub, payload, **kwargs:
                        f.calls.append((json.loads(payload), kwargs)) or 'retry')
    f.push.tasks.dispatch_one(f.devices)
    assert f.push.tasks.status(f.device)['last_task']['status'] == 'retry'
    assert not f.push.tasks.dispatch_one(f.devices)
    first_id = f.calls[0][0]['id']
    # Simulate an interrupted in-flight attempt. Another process must wait for its lease.
    with f.push.connect() as conn:
        conn.execute("UPDATE task_outbox SET status='sending', next_at=?", (f.clock[0] + 60,))
    f.push = PushStore(f.config.data_dir / 'mobile', 'https://computer.example')
    assert not f.push.tasks.dispatch_one(f.devices)
    f.clock[0] += 61
    monkeypatch.setattr(PushStore, 'send_payload', original)
    assert f.push.tasks.dispatch_one(f.devices)
    assert f.calls[-1][0]['id'] == first_id
    assert f.runs.get_run(run.id).status == 'completed'
    assert len(f.source.recent()) == 1


def test_long_outage_preserves_results_and_resumes_reminders_without_reexecuting_jobs(setup, monkeypatch):
    f = setup
    f.push.tasks.set_enabled(f.device, True)
    first = deliver(f)
    reconcile(f)
    original = PushStore.send_payload
    monkeypatch.setattr(PushStore, 'send_payload', lambda self, sub, payload, **kwargs:
                        f.calls.append((json.loads(payload), kwargs)) or 'retry')
    f.push.tasks.dispatch_one(f.devices)
    delivery_id = f.calls[0][0]['id']
    f.clock[0] += 6 * 3600
    second = deliver(f)
    original_results = f.source.path.read_bytes()
    # Reopen the durable queue after a long interruption; the original job runs stay complete.
    f.push = PushStore(f.config.data_dir / 'mobile', 'https://computer.example')
    reconcile(f)
    monkeypatch.setattr(PushStore, 'send_payload', original)
    assert f.push.tasks.dispatch_one(f.devices)
    assert f.calls[-1][0]['id'] == delivery_id
    assert f.push.tasks.dispatch_one(f.devices)
    assert not f.push.tasks.dispatch_one(f.devices)
    assert f.source.path.read_bytes() == original_results
    assert all(f.runs.get_run(run.id).status == 'completed' for run in (first, second))
    assert len(f.source.recent()) == 2


@pytest.mark.parametrize('action', ['disable', 'revoke', 'unsubscribe'])
def test_pending_delivery_stops_after_permission_removed(setup, action):
    f = setup
    f.push.tasks.set_enabled(f.device, True)
    deliver(f)
    reconcile(f)
    if action == 'disable':
        f.push.tasks.set_enabled(f.device, False)
    elif action == 'revoke':
        f.devices.revoke(f.device)
    else:
        f.push.unsubscribe(f.device)
    f.push.tasks.dispatch_one(f.devices)
    assert not f.calls
    if action == 'disable':
        f.clock[0] += 1
        f.push.tasks.set_enabled(f.device, True)
        reconcile(f)
        assert not f.push.tasks.dispatch_one(f.devices)


def test_event_deadline_keeps_good_subscription_and_rotation_preserves_legacy(setup):
    f = setup
    f.source.path.write_text(json.dumps({'timestamp': '2000-01-01', 'title': 'old', 'message': 'kept'}) + '\n')
    f.push.tasks.set_enabled(f.device, True)
    deliver(f)
    assert f.source.events()[0]['timestamp'] == '2000-01-01'
    assert 'event_id' not in f.source.events()[0]
    assert f.source.recent()[0].message == 'kept'
    reconcile(f)
    event = f.source.events()[-1]
    f.clock[0] += 86401
    f.source.append(Notification(channel='web_log', title='new', message='unrelated'))
    assert f.source.events()[-2] == event
    f.push.tasks.dispatch_one(f.devices)
    assert not f.calls
    assert f.push.status(f.device)['subscribed']
    assert f.push.tasks.status(f.device)['last_task']['status'] == 'expired'


def test_expired_vendor_subscription_disables_tasks_without_removing_result(setup, monkeypatch):
    f = setup
    f.push.tasks.set_enabled(f.device, True)
    deliver(f)
    reconcile(f)
    monkeypatch.setattr(PushStore, 'send_payload', lambda *args, **kwargs: 'expired')
    f.push.tasks.dispatch_one(f.devices)
    assert not f.push.status(f.device)['subscribed']
    assert not f.push.tasks.status(f.device)['tasks_enabled']
    assert f.push.tasks.status(f.device)['last_task']['status'] == 'expired'
    assert f.source.recent()[0].message == 'private research and original result'
