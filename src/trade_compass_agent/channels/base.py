"""Channel abstraction — base adapter and router.

Defines the shared contract implemented by every messaging platform adapter:
- Lifecycle: connect → send/receive → disconnect
- Message envelope: platform-agnostic ChannelMessage
- Router: fan-out to multiple adapters
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class ChannelMessage:
    """Platform-agnostic message envelope."""

    content: str
    title: str = ""
    severity: str = "info"  # info, warning, critical
    metadata: dict[str, Any] = field(default_factory=dict)
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    reply_to: str | None = None  # For bidirectional: ID of message being replied to


@dataclass
class IncomingMessage:
    """Message received from external platform."""

    platform: str
    sender_id: str
    sender_name: str
    content: str
    message_id: str
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    metadata: dict[str, Any] = field(default_factory=dict)


class ChannelAdapter(ABC):
    """Abstract base for platform channel adapters.

    Subclasses implement:
    - send(): Push a message to the platform
    - Optional: start_listening() for bidirectional communication
    """

    name: str = "base"

    @abstractmethod
    async def send(self, message: ChannelMessage) -> bool:
        """Send a message to the platform. Returns True on success."""
        ...

    async def start_listening(self, on_message: Any = None) -> None:
        """Start receiving messages (bidirectional adapters only)."""
        pass

    async def stop_listening(self) -> None:
        """Stop receiving messages."""
        pass

    def send_sync(self, message: ChannelMessage) -> bool:
        """Synchronous send wrapper for non-async contexts."""
        import asyncio
        try:
            asyncio.get_running_loop()
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                return pool.submit(asyncio.run, self.send(message)).result(timeout=10)
        except RuntimeError:
            return asyncio.run(self.send(message))


InboundHandler = Any  # Callable[[IncomingMessage], ChannelMessage | None]


class ChannelRouter:
    """Fan-out messages to all configured channel adapters + inbound routing.

    The gateway can fan out one notification to multiple registered adapters.
    """

    _ALIASES: dict[str, list[str]] = {
        "feishu": ["feishu_webhook", "feishu_bot"],
        "wecom": ["wecom_webhook", "wecom_bot"],
        "weixin": ["weixin_bot"],
    }

    def __init__(self) -> None:
        self._adapters: list[ChannelAdapter] = []
        self._inbound_handler: InboundHandler | None = None

    def register(self, adapter: ChannelAdapter) -> None:
        self._adapters.append(adapter)
        logger.info("Channel registered: %s", adapter.name)

    def set_inbound_handler(self, handler: InboundHandler) -> None:
        """Set the handler for inbound messages (IncomingMessage → ChannelMessage reply)."""
        self._inbound_handler = handler

    @property
    def adapters(self) -> list[ChannelAdapter]:
        return list(self._adapters)

    def get_adapter(self, name: str) -> ChannelAdapter | None:
        """Look up an adapter by name, with alias resolution."""
        for a in self._adapters:
            if a.name == name:
                return a
        candidates = self._ALIASES.get(name, [])
        for candidate in candidates:
            for a in self._adapters:
                if a.name == candidate:
                    return a
        return None

    async def handle_inbound(self, message: IncomingMessage) -> ChannelMessage | None:
        """Route an inbound message to the handler and return the reply."""
        if not self._inbound_handler:
            logger.warning("No inbound handler registered, dropping message from %s", message.platform)
            return None
        try:
            return self._inbound_handler(message)
        except Exception as exc:
            logger.error("Inbound handler error: %s", exc)
            return None

    async def broadcast(self, message: ChannelMessage) -> dict[str, bool]:
        """Send message to all registered adapters. Returns {adapter_name: success}."""
        results: dict[str, bool] = {}
        for adapter in self._adapters:
            try:
                ok = await adapter.send(message)
                results[adapter.name] = ok
            except Exception as exc:
                logger.error("Channel %s failed: %s", adapter.name, exc)
                results[adapter.name] = False
        return results

    def broadcast_sync(self, message: ChannelMessage) -> dict[str, bool]:
        """Synchronous broadcast for non-async job contexts."""
        results: dict[str, bool] = {}
        for adapter in self._adapters:
            try:
                ok = adapter.send_sync(message)
                results[adapter.name] = ok
            except Exception as exc:
                logger.error("Channel %s failed: %s", adapter.name, exc)
                results[adapter.name] = False
        return results
