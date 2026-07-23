"""Notification channels — multi-platform message delivery + bidirectional chat.

Channel adapters share a common asynchronous lifecycle and message contract:
- Abstract base with send/receive lifecycle
- Per-platform adapters (Feishu, WeCom, WeChat, Webhook)
- ChannelRouter dispatches to configured adapters
- GatewayDaemon manages persistent bidirectional connections
- Inbound routing: external messages → agent → reply
"""

from trade_compass_agent.channels.base import (
    ChannelAdapter,
    ChannelMessage,
    ChannelRouter,
    IncomingMessage,
)
from trade_compass_agent.channels.webhook import WebhookAdapter

__all__ = [
    "ChannelAdapter",
    "ChannelMessage",
    "ChannelRouter",
    "IncomingMessage",
    "WebhookAdapter",
]
