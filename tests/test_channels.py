"""Tests for channel adapters."""

import asyncio
import stat
from pathlib import Path


from trade_compass_agent.channels.base import ChannelMessage, ChannelRouter
from trade_compass_agent.channels.feishu import FeishuWebhookAdapter
from trade_compass_agent.channels.wecom import WecomWebhookAdapter
from trade_compass_agent.channels.weixin import WeixinBotAdapter
from trade_compass_agent.channels.webhook import WebhookAdapter


class TestChannelMessage:
    def test_message_creation(self):
        msg = ChannelMessage(content="test", title="title", severity="warning")
        assert msg.content == "test"
        assert msg.title == "title"
        assert msg.severity == "warning"
        assert msg.timestamp  # auto-generated

    def test_message_defaults(self):
        msg = ChannelMessage(content="hello")
        assert msg.severity == "info"
        assert msg.title == ""
        assert msg.metadata == {}


class TestChannelRouter:
    def test_register_adapter(self):
        router = ChannelRouter()
        adapter = WebhookAdapter(url="http://example.com")
        router.register(adapter)
        assert len(router.adapters) == 1
        assert router.adapters[0].name == "webhook"

    def test_broadcast_no_adapters(self):
        router = ChannelRouter()
        msg = ChannelMessage(content="test")
        results = asyncio.run(router.broadcast(msg))
        assert results == {}

    def test_broadcast_sync_no_adapters(self):
        router = ChannelRouter()
        msg = ChannelMessage(content="test")
        results = router.broadcast_sync(msg)
        assert results == {}


class TestFeishuWebhook:
    def test_no_url_returns_false(self):
        adapter = FeishuWebhookAdapter(webhook_url="")
        msg = ChannelMessage(content="test")
        result = asyncio.run(adapter.send(msg))
        assert result is False

    def test_payload_format(self):
        adapter = FeishuWebhookAdapter(webhook_url="http://fake")
        msg = ChannelMessage(content="hello", title="Alert", severity="critical")
        payload = adapter._build_payload(msg)
        assert payload["msg_type"] == "interactive"
        assert payload["card"]["header"]["template"] == "red"


class TestWecomWebhook:
    def test_no_url_returns_false(self):
        adapter = WecomWebhookAdapter(webhook_url="")
        msg = ChannelMessage(content="test")
        result = asyncio.run(adapter.send(msg))
        assert result is False

    def test_payload_format(self):
        adapter = WecomWebhookAdapter(webhook_url="http://fake")
        msg = ChannelMessage(content="hello", title="Alert")
        payload = adapter._build_payload(msg)
        assert payload["msgtype"] == "markdown"
        assert "Alert" in payload["markdown"]["content"]


class TestWebhookAdapter:
    def test_no_url_returns_false(self):
        adapter = WebhookAdapter(url="")
        msg = ChannelMessage(content="test")
        result = asyncio.run(adapter.send(msg))
        assert result is False


def test_weixin_credentials_are_owner_only(tmp_path: Path) -> None:
    path = tmp_path / "weixin.json"
    adapter = WeixinBotAdapter(credentials_path=str(path))
    adapter._token = "sensitive-token"

    adapter._save_credentials()

    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_weixin_existing_credentials_are_hardened_on_load(tmp_path: Path) -> None:
    path = tmp_path / "weixin.json"
    path.write_text('{"token": "sensitive-token"}', encoding="utf-8")
    path.chmod(0o644)

    adapter = WeixinBotAdapter(credentials_path=str(path))
    adapter._load_credentials()

    assert adapter._token == "sensitive-token"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
