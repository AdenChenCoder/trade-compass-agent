"""Legacy inbound message parsers retained for future verified callbacks.

Supports:
- Feishu event subscription (URL verification + message events)
- WeCom callback (URL verification + message events)

These parsers are not exposed by the HTTP API because they do not verify platform
signatures. Production bidirectional messaging uses authenticated gateway connections.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from trade_compass_agent.channels.base import IncomingMessage

logger = logging.getLogger(__name__)


def handle_platform_callback(platform: str, body: dict[str, Any]) -> str | None:
    """Dispatch a platform callback to the appropriate handler.

    Returns reply text or None if the message should be ignored.
    """
    if platform == "feishu":
        return _handle_feishu(body)
    if platform == "wecom":
        return _handle_wecom(body)
    logger.warning("Unknown inbound platform: %s", platform)
    return None


def _handle_feishu(body: dict[str, Any]) -> str | None:
    """Handle Feishu event subscription callback.

    Supports:
    - URL verification challenge
    - im.message.receive_v1 events (text messages)
    """
    if "challenge" in body:
        return json.dumps({"challenge": body["challenge"]})

    header = body.get("header", {})
    event_type = header.get("event_type", "")

    if event_type != "im.message.receive_v1":
        return None

    event = body.get("event", {})
    message = event.get("message", {})
    msg_type = message.get("message_type", "")
    if msg_type != "text":
        return None

    content_raw = message.get("content", "{}")
    try:
        content = json.loads(content_raw).get("text", "")
    except (json.JSONDecodeError, AttributeError):
        content = content_raw

    # Strip @mention prefix
    content = content.strip()
    if not content:
        return None

    sender = event.get("sender", {}).get("sender_id", {})
    incoming = IncomingMessage(
        platform="feishu",
        sender_id=sender.get("user_id", ""),
        sender_name=sender.get("user_id", ""),
        content=content,
        message_id=message.get("message_id", ""),
        metadata={
            "chat_id": message.get("chat_id", ""),
            "chat_type": message.get("chat_type", ""),
        },
    )

    return _route_to_agent(incoming)


def _handle_wecom(body: dict[str, Any]) -> str | None:
    """Handle WeCom callback message.

    Supports:
    - URL verification (echostr)
    - Text messages
    """
    if "echostr" in body:
        return body["echostr"]

    msg_type = body.get("MsgType", "")
    if msg_type != "text":
        return None

    content = body.get("Content", "").strip()
    if not content:
        return None

    incoming = IncomingMessage(
        platform="wecom",
        sender_id=body.get("FromUserName", ""),
        sender_name=body.get("FromUserName", ""),
        content=content,
        message_id=body.get("MsgId", ""),
        metadata={"agent_id": body.get("AgentID", "")},
    )

    return _route_to_agent(incoming)


def _route_to_agent(message: IncomingMessage) -> str:
    """Route an inbound message to the agent for processing.

    Uses AgentLoop.run_turn with per-user session isolation.
    """
    from trade_compass_agent.config import load_app_config
    from trade_compass_agent.runtime.loop import AgentLoop

    from trade_compass_agent.channels.gateway import _channel_session_id

    session_id = _channel_session_id(message)

    try:
        config = load_app_config()
        agent = AgentLoop.from_config(config)
        response = agent.run_turn(message.content, session_id=session_id)
        return response.summary or "（分析完成，无文本输出）"
    except Exception as exc:
        logger.error("Agent processing failed for inbound message: %s", exc)
        return f"处理消息时出错: {exc}"
