"""WeCom (企业微信) channel adapter — webhook push + AI bot WebSocket bidirectional.

Supports two modes:
1. Webhook push: POST to WeCom group robot webhook
2. Bot bidirectional: WebSocket to wss://openws.work.weixin.qq.com (AI Bot gateway)

"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any

import httpx

from trade_compass_agent.channels.base import (
    ChannelAdapter,
    ChannelMessage,
    IncomingMessage,
)

logger = logging.getLogger(__name__)

_WECOM_WS_URL = "wss://openws.work.weixin.qq.com"
_HEARTBEAT_INTERVAL = 30  # seconds
_DEDUP_WINDOW = 300  # seconds


class WecomWebhookAdapter(ChannelAdapter):
    """Push-only adapter using WeCom group robot webhook.

    Env vars:
    - WECOM_WEBHOOK_URL: Robot webhook URL (includes key param)
    """

    name = "wecom_webhook"

    def __init__(self, webhook_url: str | None = None) -> None:
        self.webhook_url = webhook_url or os.environ.get("WECOM_WEBHOOK_URL", "")

    async def send(self, message: ChannelMessage) -> bool:
        if not self.webhook_url:
            logger.warning("WeCom webhook URL not configured")
            return False

        payload = self._build_payload(message)
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(self.webhook_url, json=payload)
                if resp.status_code == 200:
                    body = resp.json()
                    if body.get("errcode") == 0:
                        return True
                    logger.warning("WeCom webhook error: %s", body.get("errmsg"))
                    return False
                logger.warning("WeCom webhook HTTP %d", resp.status_code)
                return False
        except Exception as exc:
            logger.error("WeCom webhook send failed: %s", exc)
            return False

    def _build_payload(self, message: ChannelMessage) -> dict[str, Any]:
        """Build WeCom markdown message payload."""
        content = message.content
        if message.title:
            content = f"## {message.title}\n{content}"

        return {
            "msgtype": "markdown",
            "markdown": {"content": content},
        }


class WecomBotAdapter(ChannelAdapter):
    """Bidirectional WeCom AI Bot via WebSocket gateway.

    Connects to wss://openws.work.weixin.qq.com and authenticates via
    aibot_subscribe. Supports DM + group messaging.

    Env vars:
    - WECOM_BOT_ID: AI Bot ID
    - WECOM_SECRET: AI Bot Secret
    """

    name = "wecom_bot"

    def __init__(
        self,
        bot_id: str | None = None,
        secret: str | None = None,
        ws_url: str | None = None,
    ) -> None:
        self.bot_id = bot_id or os.environ.get("WECOM_BOT_ID", "")
        self.secret = secret or os.environ.get("WECOM_SECRET", "")
        self.ws_url = ws_url or os.environ.get("WECOM_WEBSOCKET_URL", _WECOM_WS_URL)
        self._on_message: Any = None
        self._ws: Any = None
        self._running = False
        self._seen: dict[str, float] = {}  # msg dedup

    async def send(self, message: ChannelMessage) -> bool:
        """Send reply via WebSocket or proactive API."""
        if not self._ws:
            logger.warning("WeCom bot: WebSocket not connected")
            return False

        reply_to = message.metadata.get("reply_msg_id")
        user_id = message.metadata.get("user_id", "")
        chat_id = message.metadata.get("chat_id", "")

        # Use passive reply if we have a message context
        if reply_to:
            return await self._ws_send_cmd("aibot_respond_msg", {
                "msg_id": reply_to,
                "content": message.content,
            })

        # Proactive send
        target: dict[str, Any] = {"content": message.content}
        if chat_id:
            target["chatid"] = chat_id
        elif user_id:
            target["userid"] = user_id
        else:
            logger.warning("WeCom bot: no target for proactive message")
            return False

        return await self._ws_send_cmd("aibot_send_msg", target)

    async def start_listening(self, on_message: Any = None) -> None:
        """Connect to WeCom WebSocket gateway and listen for messages.

        This method blocks while the connection is alive.
        """
        self._on_message = on_message
        self._running = True

        try:
            import aiohttp
        except ImportError:
            logger.error("aiohttp not installed; WeCom bot requires: pip install aiohttp")
            return

        if not self.bot_id or not self.secret:
            logger.error("WECOM_BOT_ID / WECOM_SECRET not set")
            return

        logger.info("WeCom bot: connecting to %s", self.ws_url)

        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(self.ws_url, heartbeat=_HEARTBEAT_INTERVAL) as ws:
                self._ws = ws

                # Authenticate
                ok = await self._subscribe()
                if not ok:
                    logger.error("WeCom bot: subscription failed")
                    return

                logger.info("WeCom bot: connected and subscribed")

                # Start heartbeat task
                hb_task = asyncio.create_task(self._heartbeat_loop())

                try:
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            await self._handle_frame(msg.data)
                        elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            break
                finally:
                    hb_task.cancel()
                    self._ws = None

    async def stop_listening(self) -> None:
        self._running = False
        if self._ws is not None:
            await self._ws.close()
            self._ws = None
        logger.info("WeCom bot: disconnected")

    async def _subscribe(self) -> bool:
        """Send aibot_subscribe authentication frame."""
        frame = json.dumps({
            "cmd": "aibot_subscribe",
            "data": {
                "bot_id": self.bot_id,
                "secret": self.secret,
            },
        })
        await self._ws.send_str(frame)

        # Wait for ack
        try:
            resp = await asyncio.wait_for(self._ws.receive(), timeout=10)
            if resp.type == 1:  # TEXT
                data = json.loads(resp.data)
                errcode = data.get("errcode", -1)
                if errcode == 0:
                    return True
                logger.error("WeCom subscribe error: %s", data)
            return False
        except asyncio.TimeoutError:
            logger.error("WeCom subscribe timeout")
            return False

    async def _heartbeat_loop(self) -> None:
        """Send application-level ping every 30s."""
        consecutive_failures = 0
        while self._running and self._ws:
            await asyncio.sleep(_HEARTBEAT_INTERVAL)
            try:
                await self._ws.ping()
                consecutive_failures = 0
            except Exception:
                consecutive_failures += 1
                if consecutive_failures >= 3:
                    logger.warning("WeCom bot: 3 heartbeat failures, closing for reconnect")
                    await self._ws.close()
                    break

    async def _handle_frame(self, raw: str) -> None:
        """Parse an inbound WebSocket text frame."""
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return

        cmd = data.get("cmd", "")
        if cmd == "aibot_message":
            await self._handle_message(data.get("data", {}))
        elif cmd == "pong":
            pass  # heartbeat ack

    async def _handle_message(self, data: dict[str, Any]) -> None:
        """Process an inbound aibot_message."""
        msg_id = data.get("msg_id", "")
        if self._is_duplicate(msg_id):
            return

        msg_type = data.get("msg_type", "text")
        if msg_type != "text":
            logger.debug("WeCom bot: ignoring msg_type=%s", msg_type)
            return

        content = data.get("content", "").strip()
        if not content:
            return

        user_id = data.get("from_userid", "")
        chat_id = data.get("chatid", "")
        chat_type = "group" if chat_id else "p2p"

        incoming = IncomingMessage(
            platform="wecom_bot",
            sender_id=user_id,
            sender_name=data.get("from_username", user_id),
            content=content,
            message_id=msg_id,
            metadata={
                "user_id": user_id,
                "chat_id": chat_id,
                "chat_type": chat_type,
                "reply_msg_id": msg_id,
            },
        )

        if self._on_message:
            asyncio.create_task(self._on_message(incoming))

    async def _ws_send_cmd(self, cmd: str, data: dict[str, Any]) -> bool:
        """Send a command frame via WebSocket."""
        if not self._ws:
            return False
        try:
            frame = json.dumps({"cmd": cmd, "data": data}, ensure_ascii=False)
            await self._ws.send_str(frame)
            return True
        except Exception as exc:
            logger.error("WeCom bot: send %s failed: %s", cmd, exc)
            return False

    def _is_duplicate(self, msg_id: str) -> bool:
        now = time.time()
        cutoff = now - _DEDUP_WINDOW
        self._seen = {k: v for k, v in self._seen.items() if v > cutoff}
        if msg_id in self._seen:
            return True
        self._seen[msg_id] = now
        return False
