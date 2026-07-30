"""WeChat personal account adapter via iLink Bot API.

Uses Tencent's official iLink protocol (released March 2026) for bidirectional
personal WeChat messaging. QR code login, HTTP long-polling for receiving,
REST API for sending.

Protocol reference: https://github.com/epiral/weixin-bot/blob/main/PROTOCOL.md
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from pathlib import Path
from typing import Any

import httpx

from trade_compass_agent.channels.base import (
    ChannelAdapter,
    ChannelMessage,
    IncomingMessage,
)

logger = logging.getLogger(__name__)

_ILINK_BASE = "https://ilinkai.weixin.qq.com"
_QR_ENDPOINT = "/ilink/bot/get_bot_qrcode"
_QR_STATUS_ENDPOINT = "/ilink/bot/get_qrcode_status"
_GETUPDATES_ENDPOINT = "/ilink/bot/getupdates"
_SENDMESSAGE_ENDPOINT = "/ilink/bot/sendmessage"
_SENDTYPING_ENDPOINT = "/ilink/bot/sendtyping"
_GETCONFIG_ENDPOINT = "/ilink/bot/getconfig"
_LONG_POLL_TIMEOUT = 35
_CHANNEL_VERSION = "1.0.3"


class WeixinBotAdapter(ChannelAdapter):
    """Bidirectional personal WeChat bot via iLink protocol.

    Login via QR code, then long-poll for messages and reply via REST API.
    Credentials are persisted to disk for session reuse across restarts.
    """

    name = "weixin_bot"

    def __init__(self, credentials_path: str | None = None) -> None:
        default_path = os.environ.get(
            "WEIXIN_CREDENTIALS_PATH",
            str(Path("data") / "weixin_credentials.json"),
        )
        self._cred_path = Path(credentials_path or default_path)
        self._token: str = ""
        self._base_url: str = _ILINK_BASE
        self._bot_id: str = ""
        self._cursor: str = ""  # get_updates_buf
        self._context_tokens: dict[str, str] = {}  # user_id -> context_token
        self._subscriber_users: set[str] = set()  # user_ids for proactive push
        self._on_message: Any = None
        self._running = False
        self._http: httpx.AsyncClient | None = None

    @property
    def is_logged_in(self) -> bool:
        return bool(self._token)

    async def send(self, message: ChannelMessage) -> bool:
        """Send a text reply to a WeChat user.

        If no user_id in metadata, broadcasts to all subscribers.
        """
        user_id = message.metadata.get("user_id", "")

        if user_id:
            return await self._send_to_user(user_id, message)

        if self._subscriber_users:
            ok = False
            for uid in list(self._subscriber_users):
                if await self._send_to_user(uid, message):
                    ok = True
            return ok

        logger.warning("WeChat: no user_id or subscribers for push")
        return False

    async def _send_to_user(self, user_id: str, message: ChannelMessage) -> bool:
        context_token = message.metadata.get("context_token", "")
        if not context_token:
            context_token = self._context_tokens.get(user_id, "")
        if not context_token:
            logger.warning(
                "WeChat: no context_token for requested user (known_users=%d)",
                len(self._context_tokens),
            )
            return False

        text = message.content
        if message.title:
            text = f"【{message.title}】\n\n{text}"
        if len(text) > 4000:
            text = text[:4000] + "\n\n... (消息过长已截断)"

        client_id = f"bot-{uuid.uuid4().hex[:12]}"
        payload = {
            "msg": {
                "from_user_id": "",
                "to_user_id": user_id,
                "client_id": client_id,
                "message_type": 2,  # BOT
                "message_state": 2,  # FINISH
                "context_token": context_token,
                "item_list": [
                    {"type": 1, "text_item": {"text": text}},
                ],
            },
            "base_info": {"channel_version": _CHANNEL_VERSION},
        }

        try:
            resp = await self._send_api_post(payload)
            ret = resp.get("ret", -1)
            if ret == 0:
                return True
            logger.warning(
                "WeChat sendmessage error: ret=%s response_keys=%s payload_keys=%s",
                ret,
                sorted(resp),
                sorted(payload["msg"]),
            )
            return False
        except Exception as exc:
            logger.error("WeChat send failed: %s", exc)
            return False

    async def _send_api_post(self, payload: dict[str, Any]) -> dict[str, Any]:
        """POST to sendmessage with a fresh httpx client (avoids event-loop issues).

        Uses the same auth headers as getupdates (which works).
        """
        import base64
        import random

        url = f"{self._base_url}{_SENDMESSAGE_ENDPOINT}"
        headers: dict[str, str] = {
            "Content-Type": "application/json",
            "AuthorizationType": "ilink_bot_token",
            "Authorization": f"Bearer {self._token}",
            "X-WECHAT-UIN": base64.b64encode(
                str(random.randint(0, 2**32)).encode()
            ).decode(),
        }

        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(url, json=payload, headers=headers)
            logger.debug("WeChat sendmessage: status=%s", resp.status_code)
            resp.raise_for_status()
            if not resp.text.strip():
                logger.warning("WeChat sendmessage: empty body status=%s", resp.status_code)
                return {"ret": -1}
            data = resp.json()
            if not data or data.get("ret", 0) != 0:
                logger.warning(
                    "WeChat sendmessage: status=%s ret=%s errcode=%s response_keys=%s",
                    resp.status_code,
                    data.get("ret"),
                    data.get("errcode"),
                    sorted(data),
                )
            return data

    async def start_listening(self, on_message: Any = None) -> None:
        """Login (if needed) and start long-polling for messages.

        This method blocks while the polling loop is active.
        """
        self._on_message = on_message
        self._running = True

        self._http = httpx.AsyncClient(timeout=httpx.Timeout(_LONG_POLL_TIMEOUT + 10, connect=10))

        self._load_credentials()
        if not self._token:
            logged_in = await self._login()
            if not logged_in:
                logger.error("WeChat: login failed")
                return

        logger.info("WeChat bot: starting long-poll loop")

        backoff_idx = 0
        backoff_steps = [1, 2, 5, 10, 30]
        while self._running:
            try:
                msgs = await self._poll_updates()
                backoff_idx = 0
                for msg in msgs:
                    await self._handle_message(msg)
            except _SessionExpiredError:
                logger.warning("WeChat: session expired, re-login required")
                self._token = ""
                self._save_credentials()
                logged_in = await self._login()
                if not logged_in:
                    logger.error("WeChat: re-login failed, stopping")
                    break
            except asyncio.CancelledError:
                break
            except Exception as exc:
                delay = backoff_steps[min(backoff_idx, len(backoff_steps) - 1)]
                logger.warning("WeChat poll error: %s, retry in %ds", exc, delay)
                backoff_idx += 1
                await asyncio.sleep(delay)

    async def stop_listening(self) -> None:
        self._running = False
        if self._http:
            await self._http.aclose()
            self._http = None
        logger.info("WeChat bot: stopped")

    # -- Login flow --

    async def _login(self) -> bool:
        """QR code login flow using iLink protocol."""
        logger.info("WeChat: initiating QR code login...")
        try:
            qr_resp = await self._api_get(f"{_QR_ENDPOINT}?bot_type=3")
            if qr_resp.get("ret", -1) != 0:
                logger.error(
                    "WeChat: QR code request failed: ret=%s errcode=%s",
                    qr_resp.get("ret"),
                    qr_resp.get("errcode"),
                )
                return False

            qr_url = qr_resp.get("qrcode_img_content", "")
            qrcode_id = qr_resp.get("qrcode", "")
            if not qr_url or not qrcode_id:
                logger.error(
                    "WeChat: incomplete QR response (keys=%s)",
                    sorted(qr_resp),
                )
                return False

            logger.info("WeChat: scan QR code to login: %s", qr_url)
            print(f"\n{'='*60}")
            print(f"微信扫码登录: {qr_url}")
            print(f"{'='*60}\n")

            # Poll scan status via GET with required header
            for _ in range(120):  # 4 min timeout (2s * 120)
                await asyncio.sleep(2)
                status = await self._api_get(
                    f"{_QR_STATUS_ENDPOINT}?qrcode={qrcode_id}",
                    extra_headers={"iLink-App-ClientVersion": "1"},
                    timeout=40,
                )

                state = status.get("status", "")
                if state == "scaned":
                    logger.info("WeChat: QR scanned, waiting for confirmation...")
                elif state == "confirmed":
                    self._token = status.get("bot_token", "")
                    self._base_url = status.get("baseurl", _ILINK_BASE)
                    self._bot_id = status.get("ilink_bot_id", "")
                    if self._token:
                        self._save_credentials()
                        logger.info("WeChat: login successful, bot_id=%s", self._bot_id)
                        return True
                    logger.warning(
                        "WeChat: confirmed but no token (keys=%s)",
                        sorted(status),
                    )
                    return False
                elif state == "expired":
                    logger.warning("WeChat: QR code expired")
                    return False

            logger.warning("WeChat: login timeout (QR code not scanned)")
            return False
        except Exception as exc:
            logger.error("WeChat login error: %s", exc)
            return False

    # -- Long-polling --

    async def _poll_updates(self) -> list[dict[str, Any]]:
        """Single long-poll request. Returns list of messages."""
        payload = {
            "get_updates_buf": self._cursor,
            "base_info": {"channel_version": _CHANNEL_VERSION},
        }
        resp = await self._api_post(_GETUPDATES_ENDPOINT, payload)

        ret = resp.get("ret", 0)
        errcode = resp.get("errcode", 0)
        if errcode == -14:
            raise _SessionExpiredError()
        if ret != 0:
            logger.debug("WeChat getupdates: ret=%s", ret)
            return []

        new_cursor = resp.get("get_updates_buf", "")
        if new_cursor:
            self._cursor = new_cursor

        return resp.get("msgs", [])

    async def _handle_message(self, msg: dict[str, Any]) -> None:
        """Process a single inbound message."""
        msg_type = msg.get("message_type", 0)
        if msg_type != 1:  # Only handle USER messages
            return

        msg_state = msg.get("message_state", 0)
        if msg_state != 2:  # FINISH
            return

        user_id = msg.get("from_user_id", "")
        context_token = msg.get("context_token", "")

        if user_id and context_token:
            self._context_tokens[user_id] = context_token
            self._subscriber_users.add(user_id)
            self._save_credentials()

        # Extract text
        items = msg.get("item_list", [])
        text_parts: list[str] = []
        for item in items:
            item_type = item.get("type", 0)
            if item_type == 1:  # TEXT
                text_item = item.get("text_item", {})
                text_parts.append(text_item.get("text", ""))

        content = " ".join(text_parts).strip()
        if not content:
            return

        incoming = IncomingMessage(
            platform="weixin_bot",
            sender_id=user_id,
            sender_name=user_id,
            content=content,
            message_id=str(msg.get("message_id", "")),
            metadata={
                "user_id": user_id,
                "context_token": context_token,
            },
        )

        if self._on_message:
            asyncio.create_task(self._on_message(incoming))

    # -- HTTP helpers --

    async def _api_get(
        self,
        path: str,
        extra_headers: dict[str, str] | None = None,
        timeout: float = 15,
    ) -> dict[str, Any]:
        """GET from iLink API (path includes query string)."""
        url = f"{self._base_url}{path}"
        headers: dict[str, str] = {}
        if self._token:
            headers["Authorization"] = f"ilink_bot_token {self._token}"
        if extra_headers:
            headers.update(extra_headers)

        client = self._http or httpx.AsyncClient(timeout=httpx.Timeout(timeout + 5, connect=10))
        resp = await client.get(url, headers=headers)
        resp.raise_for_status()
        return resp.json()

    async def _api_post(self, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
        """POST to iLink API (getupdates etc.). Uses original auth format."""
        import base64
        import random

        url = f"{self._base_url}{endpoint}"
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self._token:
            headers["AuthorizationType"] = "ilink_bot_token"
            headers["Authorization"] = f"Bearer {self._token}"
            uin = base64.b64encode(str(random.randint(0, 2**32)).encode()).decode()
            headers["X-WECHAT-UIN"] = uin

        client = self._http or httpx.AsyncClient(timeout=httpx.Timeout(_LONG_POLL_TIMEOUT + 10, connect=10))
        resp = await client.post(url, json=payload, headers=headers)
        resp.raise_for_status()
        return resp.json()

    # -- Credential persistence --

    def _save_credentials(self) -> None:
        self._cred_path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "token": self._token,
            "base_url": self._base_url,
            "bot_id": self._bot_id,
            "cursor": self._cursor,
            "context_tokens": self._context_tokens,
        }
        self._cred_path.write_text(json.dumps(data, ensure_ascii=False, indent=2))
        self._cred_path.chmod(0o600)

    def _load_credentials(self) -> None:
        if not self._cred_path.exists():
            return
        try:
            self._cred_path.chmod(0o600)
            data = json.loads(self._cred_path.read_text())
            self._token = data.get("token", "")
            self._base_url = data.get("base_url", _ILINK_BASE)
            self._bot_id = data.get("bot_id", "")
            self._cursor = data.get("cursor", "")
            self._context_tokens = data.get("context_tokens", {})
            if self._token:
                logger.info("WeChat: loaded saved credentials for bot_id=%s", self._bot_id)
        except Exception as exc:
            logger.warning("WeChat: failed to load credentials: %s", exc)


class _SessionExpiredError(Exception):
    pass
