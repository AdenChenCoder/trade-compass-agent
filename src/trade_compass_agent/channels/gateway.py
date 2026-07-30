"""Gateway daemon — manages bidirectional connections to messaging platforms.

Maintains persistent connections (WebSocket / long-polling) and routes
inbound messages to AgentLoop, sending replies back through the originating
platform adapter.

The daemon owns adapter lifecycle and routes inbound messages to the agent.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from trade_compass_agent.channels.base import ChannelAdapter, ChannelMessage, IncomingMessage

if TYPE_CHECKING:
    from trade_compass_agent.runtime.loop import AgentLoop

logger = logging.getLogger(__name__)


class BidirectionalAdapter(Protocol):
    """Protocol for adapters that support receive + reply."""

    name: str

    async def send(self, message: ChannelMessage) -> bool: ...
    async def start_listening(self, on_message: Any = None) -> None: ...
    async def stop_listening(self) -> None: ...


@dataclass
class PlatformConnection:
    adapter: ChannelAdapter
    connected: bool = False
    last_error: str | None = None
    started_at: float | None = None


MessageHandler = Any  # Callable[[IncomingMessage], Awaitable[str]]


class GatewayDaemon:
    """Manages lifecycle of all bidirectional platform connections."""

    def __init__(self, on_message: MessageHandler | None = None) -> None:
        self._connections: dict[str, PlatformConnection] = {}
        self._on_message = on_message
        self._tasks: list[asyncio.Task] = []
        self._running = False

    def register(self, adapter: ChannelAdapter) -> None:
        self._connections[adapter.name] = PlatformConnection(adapter=adapter)
        logger.info("Gateway: registered platform %s", adapter.name)

    def set_message_handler(self, handler: MessageHandler) -> None:
        self._on_message = handler

    @property
    def platforms(self) -> dict[str, PlatformConnection]:
        return dict(self._connections)

    async def start(self) -> None:
        """Start all registered platform connections."""
        if self._running:
            return
        self._running = True
        logger.info("Gateway daemon starting with %d platform(s)", len(self._connections))
        for name, conn in self._connections.items():
            task = asyncio.create_task(self._run_platform(name, conn), name=f"gw-{name}")
            self._tasks.append(task)

    async def stop(self) -> None:
        """Gracefully stop all connections."""
        self._running = False
        for name, conn in self._connections.items():
            try:
                await conn.adapter.stop_listening()
                conn.connected = False
            except Exception as exc:
                logger.warning("Error stopping %s: %s", name, exc)
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        logger.info("Gateway daemon stopped")

    async def _run_platform(self, name: str, conn: PlatformConnection) -> None:
        """Run a single platform connection with auto-reconnect."""
        backoff = [2, 5, 10, 30, 60]
        attempt = 0
        while self._running:
            started = time.time()
            try:
                conn.started_at = started
                conn.connected = True
                conn.last_error = None
                await conn.adapter.start_listening(on_message=self._dispatch)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                conn.last_error = str(exc)
                logger.warning("Gateway: %s error: %s", name, exc)

            conn.connected = False
            elapsed = time.time() - started

            if elapsed < 5:
                # start_listening returned too quickly (e.g. login failure)
                delay = backoff[min(attempt, len(backoff) - 1)]
                attempt += 1
                logger.info("Gateway: %s returned quickly (%.1fs), backoff %ds", name, elapsed, delay)
                await asyncio.sleep(delay)
            else:
                attempt = 0
                logger.info("Gateway: %s disconnected after %.0fs, reconnecting...", name, elapsed)
                await asyncio.sleep(2)

    async def _dispatch(self, message: IncomingMessage) -> None:
        """Route inbound message to the agent and reply."""
        logger.info(
            "Gateway: received message on %s (len=%d)",
            message.platform,
            len(message.content),
        )

        if not self._on_message:
            logger.warning("Gateway: no message handler, dropping message from %s", message.platform)
            return

        try:
            reply_text = await self._on_message(message)
        except Exception as exc:
            logger.error("Gateway: agent error for %s: %s", message.platform, exc, exc_info=True)
            reply_text = f"处理消息时出错: {type(exc).__name__}"

        conn = self._connections.get(message.platform)
        if not conn:
            for name, c in self._connections.items():
                if name.startswith(message.platform):
                    conn = c
                    break

        if conn and reply_text:
            reply = ChannelMessage(
                content=reply_text,
                metadata=message.metadata,
                reply_to=message.message_id,
            )
            try:
                ok = await conn.adapter.send(reply)
                logger.info("Gateway: reply sent on %s: ok=%s (len=%d)", message.platform, ok, len(reply_text))
            except Exception as exc:
                logger.error("Gateway: reply failed on %s: %s", message.platform, exc)


# ---------------------------------------------------------------------------
# Module-level active gateway reference for adapter reuse (delivery push)
# ---------------------------------------------------------------------------

_active_gateway: GatewayDaemon | None = None


def set_active_gateway(gw: GatewayDaemon | None) -> None:
    global _active_gateway
    _active_gateway = gw


def get_gateway_adapters() -> dict[str, ChannelAdapter]:
    """Return adapters from the active gateway (for reuse in delivery)."""
    if _active_gateway is None:
        return {}
    return {name: conn.adapter for name, conn in _active_gateway.platforms.items()}


# ---------------------------------------------------------------------------
# Agent message handler — bridges IncomingMessage to AgentLoop
# ---------------------------------------------------------------------------

async def agent_message_handler(message: IncomingMessage) -> str:
    """Handle an inbound platform message using AgentLoop.run_turn."""
    import asyncio as _asyncio

    session_id = _channel_session_id(message)

    agent = _get_channel_agent()
    response = await _asyncio.to_thread(
        agent.run_turn,
        message.content,
        session_id=session_id,
    )
    return response.summary or "（分析完成，无文本输出）"


def _channel_session_id(message: IncomingMessage) -> str:
    """Return the stable logical session for one platform user."""
    return f"channel-{message.platform}-{message.sender_id}"


_channel_agent: AgentLoop | None = None
_channel_agent_lock = threading.Lock()


def _get_channel_agent() -> AgentLoop:
    from trade_compass_agent.config import load_app_config
    from trade_compass_agent.runtime.loop import AgentLoop

    global _channel_agent
    with _channel_agent_lock:
        if _channel_agent is None:
            _channel_agent = AgentLoop.from_config(load_app_config())
        return _channel_agent
