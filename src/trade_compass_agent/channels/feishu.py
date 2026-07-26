"""Feishu (Lark) channel adapter — webhook push + bot bidirectional.

Supports two modes:
1. Webhook push (simple, no deps): POST to group webhook URL
2. Bot bidirectional (requires lark_oapi): WebSocket event subscription + API reply

"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import time
from base64 import b64encode
from pathlib import Path
from typing import Any

import httpx

from trade_compass_agent.channels.base import (
    ChannelAdapter,
    ChannelMessage,
    IncomingMessage,
)

logger = logging.getLogger(__name__)


class FeishuWebhookAdapter(ChannelAdapter):
    """Push-only adapter using Feishu group bot webhook.

    Env vars:
    - FEISHU_WEBHOOK_URL: Bot webhook URL
    - FEISHU_WEBHOOK_SECRET: Optional signing secret
    """

    name = "feishu_webhook"

    def __init__(
        self,
        webhook_url: str | None = None,
        secret: str | None = None,
    ) -> None:
        self.webhook_url = webhook_url or os.environ.get("FEISHU_WEBHOOK_URL", "")
        self.secret = secret or os.environ.get("FEISHU_WEBHOOK_SECRET", "")

    async def send(self, message: ChannelMessage) -> bool:
        if not self.webhook_url:
            logger.warning("Feishu webhook URL not configured")
            return False

        payload = self._build_payload(message)
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(self.webhook_url, json=payload)
                if resp.status_code == 200:
                    body = resp.json()
                    if body.get("code") == 0:
                        return True
                    logger.warning("Feishu webhook error: %s", body.get("msg"))
                    return False
                logger.warning("Feishu webhook HTTP %d", resp.status_code)
                return False
        except Exception as exc:
            logger.error("Feishu webhook send failed: %s", exc)
            return False

    def _build_payload(self, message: ChannelMessage) -> dict[str, Any]:
        """Build Feishu webhook payload with optional signature."""
        timestamp = str(int(time.time()))

        payload: dict[str, Any] = {
            "msg_type": "interactive",
            "card": {
                "header": {
                    "title": {"tag": "plain_text", "content": message.title or "交易罗盘"},
                    "template": self._severity_color(message.severity),
                },
                "elements": [
                    {"tag": "markdown", "content": message.content},
                ],
            },
        }

        if self.secret:
            sign = self._gen_sign(timestamp)
            payload["timestamp"] = timestamp
            payload["sign"] = sign

        return payload

    def _gen_sign(self, timestamp: str) -> str:
        """Generate HMAC-SHA256 signature for webhook."""
        string_to_sign = f"{timestamp}\n{self.secret}"
        hmac_code = hmac.new(string_to_sign.encode("utf-8"), digestmod=hashlib.sha256).digest()
        return b64encode(hmac_code).decode("utf-8")

    @staticmethod
    def _severity_color(severity: str) -> str:
        return {"critical": "red", "warning": "orange", "info": "blue"}.get(severity, "blue")


class FeishuBotAdapter(ChannelAdapter):
    """Bidirectional Feishu bot adapter using lark_oapi WebSocket.

    Uses the official lark_oapi SDK to maintain a WebSocket long connection
    for receiving messages and the API for sending replies. No public URL needed.

    Env vars:
    - FEISHU_APP_ID: Bot app ID
    - FEISHU_APP_SECRET: Bot app secret
    """

    name = "feishu_bot"

    def __init__(
        self,
        app_id: str | None = None,
        app_secret: str | None = None,
        *,
        subscribers_path: Path | None = None,
    ) -> None:
        self.app_id = app_id or os.environ.get("FEISHU_APP_ID", "")
        self.app_secret = app_secret or os.environ.get("FEISHU_APP_SECRET", "")
        self._on_message: Any = None
        self._client: Any = None
        self._ws_client: Any = None
        self._main_loop: Any = None  # ref to main asyncio loop for cross-thread dispatch
        self._started_at: float = 0
        self._seen_msg_ids: dict[str, float] = {}  # dedup: msg_id -> timestamp
        self._subscribers_path = subscribers_path or self._default_subscribers_path()
        self._subscriber_chats: set[str] = self._load_subscribers()

    async def send(self, message: ChannelMessage) -> bool:
        """Send message via Feishu bot API."""
        if not self.app_id or not self.app_secret:
            logger.warning("Feishu bot credentials not configured")
            return False

        try:
            from lark_oapi.api.im.v1 import (
                CreateMessageRequest,
                CreateMessageRequestBody,
                ReplyMessageRequest,
                ReplyMessageRequestBody,
            )

            client = self._get_lark_client()

            reply_to = message.reply_to
            chat_id = message.metadata.get("chat_id", "")

            text = message.content
            if message.title:
                text = f"**{message.title}**\n\n{text}"
            content = json.dumps({"text": text}, ensure_ascii=False)

            if reply_to:
                body = ReplyMessageRequestBody.builder().msg_type("text").content(content).build()
                request = (
                    ReplyMessageRequest.builder().message_id(reply_to).request_body(body).build()
                )
                response = client.im.v1.message.reply(request)
                if not response.success():
                    logger.warning(
                        "Feishu bot reply failed: code=%s msg=%s", response.code, response.msg
                    )
                    return False
                return True
            elif chat_id:
                return self._send_to_chat(
                    client, CreateMessageRequest, CreateMessageRequestBody, chat_id, content
                )
            elif self._subscriber_chats:
                ok = False
                for cid in list(self._subscriber_chats):
                    if self._send_to_chat(
                        client, CreateMessageRequest, CreateMessageRequestBody, cid, content
                    ):
                        ok = True
                return ok
            else:
                logger.warning(
                    "Feishu bot: no chat_id, reply_to, or subscribers for push "
                    "(message the bot once, or set FEISHU_PUSH_CHAT_ID / FEISHU_WEBHOOK_URL)"
                )
                return False
        except ImportError:
            logger.warning("lark_oapi not installed, Feishu bot unavailable")
            return False
        except Exception as exc:
            logger.error("Feishu bot send failed: %s", exc)
            return False

    @staticmethod
    def _send_to_chat(
        client: Any,
        CreateMessageRequest: Any,
        CreateMessageRequestBody: Any,
        chat_id: str,
        content: str,
    ) -> bool:
        body = (
            CreateMessageRequestBody.builder()
            .receive_id(chat_id)
            .msg_type("text")
            .content(content)
            .build()
        )
        request = (
            CreateMessageRequest.builder().receive_id_type("chat_id").request_body(body).build()
        )
        response = client.im.v1.message.create(request)
        if not response.success():
            logger.warning(
                "Feishu bot send to %s failed: code=%s msg=%s", chat_id, response.code, response.msg
            )
            return False
        return True

    def _get_lark_client(self) -> Any:
        if self._client is None:
            import lark_oapi as lark

            self._client = (
                lark.Client.builder().app_id(self.app_id).app_secret(self.app_secret).build()
            )
        return self._client

    async def start_listening(self, on_message: Any = None) -> None:
        """Start WebSocket event subscription using lark_oapi SDK.

        The SDK's ws.Client.start() creates its own asyncio event loop,
        so we run it in a dedicated daemon thread to avoid conflicts.
        This method blocks (via Event) while the connection is alive.
        """
        self._on_message = on_message
        self._started_at = time.time()

        try:
            from lark_oapi.core.enum import LogLevel
            from lark_oapi.event.dispatcher_handler import EventDispatcherHandler
            from lark_oapi.ws import Client as FeishuWSClient
        except ImportError:
            logger.error("lark_oapi or websockets not installed; pip install lark-oapi websockets")
            return

        if not self.app_id or not self.app_secret:
            logger.error("FEISHU_APP_ID / FEISHU_APP_SECRET not set")
            return

        handler = (
            EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(self._on_message_event)
            .build()
        )

        self._ws_client = FeishuWSClient(
            self.app_id,
            self.app_secret,
            log_level=LogLevel.WARNING,
            event_handler=handler,
        )

        logger.info("Feishu bot: starting WebSocket connection")

        import threading

        self._main_loop = asyncio.get_running_loop()
        self._stop_event = threading.Event()

        def _run_sdk() -> None:
            # The lark_oapi SDK captures the event loop at import time
            # into a module-level `loop` variable, which is the main
            # thread's uvloop. We must replace it with a fresh loop so
            # `start()` can call `loop.run_until_complete()`.
            import lark_oapi.ws.client as _lark_ws_mod

            fresh_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(fresh_loop)
            _lark_ws_mod.loop = fresh_loop
            try:
                self._ws_client.start()
            finally:
                fresh_loop.close()

        self._ws_thread = threading.Thread(
            target=_run_sdk,
            daemon=True,
            name="feishu-ws",
        )
        self._ws_thread.start()

        # Block until stop is requested (or thread dies)
        while not self._stop_event.is_set() and self._ws_thread.is_alive():
            await asyncio.sleep(1)

    async def stop_listening(self) -> None:
        if hasattr(self, "_stop_event"):
            self._stop_event.set()
        if self._ws_client is not None:
            try:
                self._ws_client.stop()
            except Exception:
                pass
            self._ws_client = None
        logger.info("Feishu bot: WebSocket stopped")

    def _on_message_event(self, data: Any) -> None:
        """Handle im.message.receive_v1 event from SDK dispatcher.

        Called from the SDK's own thread — dispatch to the main
        asyncio loop for async handling.
        """
        try:
            event = data.event
            message = event.message
            msg_id = message.message_id

            if self._is_duplicate(msg_id):
                return

            create_time = int(message.create_time or "0")
            if create_time > 0 and create_time / 1000 < self._started_at:
                return

            msg_type = message.message_type
            if msg_type != "text":
                logger.debug("Feishu bot: ignoring msg_type=%s", msg_type)
                return

            content_raw = message.content or "{}"
            try:
                content = json.loads(content_raw).get("text", "")
            except (json.JSONDecodeError, AttributeError):
                content = content_raw

            mentions = getattr(message, "mentions", None) or []
            for mention in mentions:
                key = getattr(mention, "key", "")
                if key:
                    content = content.replace(key, "").strip()
            content = content.strip()
            if not content:
                return

            sender = event.sender
            sender_id_obj = getattr(sender, "sender_id", None)
            user_id = getattr(sender_id_obj, "open_id", "") if sender_id_obj else ""

            chat_id = message.chat_id
            chat_type = message.chat_type

            if chat_id:
                self._register_subscriber(chat_id)

            incoming = IncomingMessage(
                platform="feishu_bot",
                sender_id=user_id,
                sender_name=user_id,
                content=content,
                message_id=msg_id,
                metadata={
                    "chat_id": chat_id,
                    "chat_type": chat_type,
                },
            )

            logger.info(
                "Feishu bot: received message from %s in chat %s: %s",
                user_id,
                chat_id,
                content[:80],
            )

            if self._on_message and self._main_loop:
                asyncio.run_coroutine_threadsafe(self._on_message(incoming), self._main_loop)
        except Exception as exc:
            logger.error("Feishu bot: message handler error: %s", exc, exc_info=True)

    def _is_duplicate(self, msg_id: str) -> bool:
        """Simple dedup with 24h window."""
        now = time.time()
        # Cleanup old entries
        cutoff = now - 86400
        self._seen_msg_ids = {k: v for k, v in self._seen_msg_ids.items() if v > cutoff}

        if msg_id in self._seen_msg_ids:
            return True
        self._seen_msg_ids[msg_id] = now
        return False

    @staticmethod
    def _default_subscribers_path() -> Path:
        from trade_compass_agent.config import load_app_config

        return load_app_config().data_dir / "channel_subscribers.json"

    def _load_subscribers(self) -> set[str]:
        from trade_compass_agent.channels.subscriber_store import load_channel_subscribers

        stored = load_channel_subscribers(self._subscribers_path).get("feishu", set())
        push_chat = os.environ.get("FEISHU_PUSH_CHAT_ID", "").strip()
        if push_chat:
            stored.add(push_chat)
        if stored:
            logger.info("Feishu bot: loaded %d subscriber chat(s)", len(stored))
        return stored

    def _register_subscriber(self, chat_id: str) -> None:
        chat_id = chat_id.strip()
        if not chat_id or chat_id in self._subscriber_chats:
            return
        self._subscriber_chats.add(chat_id)
        from trade_compass_agent.channels.subscriber_store import (
            load_channel_subscribers,
            save_channel_subscribers,
        )

        all_subs = load_channel_subscribers(self._subscribers_path)
        all_subs.setdefault("feishu", set()).add(chat_id)
        save_channel_subscribers(self._subscribers_path, all_subs)
        logger.info("Feishu bot: registered subscriber chat %s", chat_id)
