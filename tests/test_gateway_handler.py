from __future__ import annotations

from unittest.mock import MagicMock, patch

import asyncio
import trade_compass_agent.channels.gateway as gateway_mod
from trade_compass_agent.channels.base import IncomingMessage
from trade_compass_agent.channels.gateway import _get_channel_agent, agent_message_handler


def test_get_channel_agent_resolves_agent_loop():
    gateway_mod._channel_agent = None
    fake_loop = MagicMock()
    with patch("trade_compass_agent.runtime.loop.AgentLoop") as mock_cls:
        mock_cls.from_config.return_value = fake_loop
        agent = _get_channel_agent()
        assert agent is fake_loop
        mock_cls.from_config.assert_called_once()
    gateway_mod._channel_agent = None


def test_agent_message_handler_no_name_error():
    gateway_mod._channel_agent = None
    fake_loop = MagicMock()
    fake_loop.run_turn.return_value = MagicMock(summary="ok")
    with patch.object(gateway_mod, "_get_channel_agent", return_value=fake_loop):
        reply = asyncio.run(
            agent_message_handler(
                IncomingMessage(
                    platform="feishu_bot",
                    sender_id="u1",
                    sender_name="u1",
                    content="hi",
                    message_id="m1",
                )
            )
        )
    assert reply == "ok"
    session_id = fake_loop.run_turn.call_args.kwargs["session_id"]
    assert session_id == "channel-feishu_bot-u1"
