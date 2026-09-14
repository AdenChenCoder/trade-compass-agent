from dataclasses import replace
from unittest.mock import Mock

import pytest

from trade_compass_agent.channels.base import ChannelRouter
from trade_compass_agent.config import load_app_config
from trade_compass_agent.ops.delivery import DeliveryRouter
from trade_compass_agent.ops.job_definition import DeliveryConfig
from trade_compass_agent.ops.run_store import SqliteRunStore


@pytest.mark.parametrize("failure", [False, RuntimeError("transport unavailable")])
def test_delivery_failure_is_visible_without_changing_analysis(client, monkeypatch, failure):
    config = load_app_config()
    config = replace(config, notifications=replace(config.notifications, macos_enabled=False))
    store = SqliteRunStore(config.data_dir / "scheduler.db")
    run = store.create_run("premarket")
    store.complete_run(run, message="完整分析结果", artifact="report.md")
    router = ChannelRouter()
    feishu = Mock(name="feishu")
    feishu.name = "feishu_bot"
    if isinstance(failure, Exception):
        feishu.send_sync.side_effect = failure
    else:
        feishu.send_sync.return_value = failure
    wecom = Mock(name="wecom")
    wecom.name = "wecom_bot"
    wecom.send_sync.return_value = True
    router.register(feishu)
    router.register(wecom)
    monkeypatch.setattr("trade_compass_agent.ops.delivery._build_channel_router", lambda: router)

    DeliveryRouter(config).deliver(run, DeliveryConfig(channels=("web_log", "feishu", "wecom")))

    # Read through the same API as the workbench, after reopening persisted state.
    notifications = client.get("/api/notifications").json()
    failures = [n for n in notifications if n["severity"] == "warning"]
    assert len(failures) == 1
    assert "飞书" in failures[0]["message"]
    assert "发送失败" in failures[0]["title"]
    assert any(n["message"] == "完整分析结果" for n in notifications)
    detail = client.get(f"/api/jobs/runs/{run.id}").json()
    assert detail["status"] == "completed"
    assert detail["message"] == "完整分析结果"
    assert detail["artifact"] == "report.md"
    assert detail["error"] is None
    steps = {s["step_id"]: s for s in detail["step_runs"]}
    assert steps["飞书消息发送"]["status"] == "failed"
    assert "请检查渠道连接和配置" in steps["飞书消息发送"]["error"]
    assert steps["企业微信消息发送"]["status"] == "completed"
    wecom.send_sync.assert_called_once()


@pytest.mark.parametrize("status,silent", [("completed", True), ("skipped", False)])
def test_silent_and_skipped_runs_do_not_send(client, monkeypatch, status, silent):
    config = load_app_config()
    config = replace(config, notifications=replace(config.notifications, macos_enabled=False))
    store = SqliteRunStore(config.data_dir / "scheduler.db")
    run = store.create_run("postmarket")
    run.status = status
    build_router = Mock()
    monkeypatch.setattr("trade_compass_agent.ops.delivery._build_channel_router", build_router)

    DeliveryRouter(config).deliver(
        run, DeliveryConfig(channels=("web_log", "feishu"), silent_on_success=silent),
    )

    build_router.assert_not_called()
    assert client.get("/api/notifications").json() == []


def test_successful_delivery_has_no_failure_notification(client, monkeypatch):
    config = load_app_config()
    config = replace(config, notifications=replace(config.notifications, macos_enabled=False))
    store = SqliteRunStore(config.data_dir / "scheduler.db")
    run = store.create_run("custom:test")
    store.complete_run(run, message="自定义任务结果")
    router = ChannelRouter()
    adapter = Mock()
    adapter.name = "feishu_bot"
    adapter.send_sync.return_value = True
    router.register(adapter)
    monkeypatch.setattr("trade_compass_agent.ops.delivery._build_channel_router", lambda: router)

    DeliveryRouter(config).deliver(run, DeliveryConfig(channels=("web_log", "feishu")))

    notifications = client.get("/api/notifications").json()
    assert len(notifications) == 1
    assert notifications[0]["severity"] == "info"
    detail = client.get(f"/api/jobs/runs/{run.id}").json()
    assert detail.get("analysis", detail["message"]) == "自定义任务结果"
    assert detail["step_runs"][0]["status"] == "completed"
    assert detail["step_runs"][0]["output"] == "任务结果已发送到飞书"
    assert adapter.send_sync.call_args.args[0].content == "自定义任务结果"
