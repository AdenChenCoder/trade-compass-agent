"""Generic webhook adapter — push to any URL.

Provides a simple, universal adapter for arbitrary webhook endpoints.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

from trade_compass_agent.channels.base import ChannelAdapter, ChannelMessage

logger = logging.getLogger(__name__)


class WebhookAdapter(ChannelAdapter):
    """Push to a generic JSON webhook endpoint.

    Env vars:
    - WEBHOOK_NOTIFICATION_URL: Target URL
    """

    name = "webhook"

    def __init__(self, url: str | None = None, headers: dict[str, str] | None = None) -> None:
        self.url = url or os.environ.get("WEBHOOK_NOTIFICATION_URL", "")
        self.headers = headers or {}

    async def send(self, message: ChannelMessage) -> bool:
        if not self.url:
            logger.debug("Webhook URL not configured, skipping")
            return False

        payload: dict[str, Any] = {
            "title": message.title,
            "content": message.content,
            "severity": message.severity,
            "timestamp": message.timestamp,
            "metadata": message.metadata,
        }

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(self.url, json=payload, headers=self.headers)
                return 200 <= resp.status_code < 300
        except Exception as exc:
            logger.error("Webhook send failed: %s", exc)
            return False
