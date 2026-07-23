"""DeliveryRouter — Job result delivery to multiple channels.

Wraps the existing ChannelRouter with Job-specific delivery logic:
- Respects DeliveryConfig per Job (channels, silent_on_success)
- Sends macOS notifications via NotificationCenter
- Records to web_log (notification store)
- Extracts full Agent analysis from step data_json for rich delivery
"""

from __future__ import annotations

import logging
import os

from trade_compass_agent.channels.base import ChannelMessage, ChannelRouter
from trade_compass_agent.config import AppConfig
from trade_compass_agent.domain import Notification
from trade_compass_agent.ops.job_definition import DeliveryConfig
from trade_compass_agent.ops.notifications import JsonNotificationStore, NotificationCenter
from trade_compass_agent.ops.run_content import extract_analysis_from_artifact, extract_analysis_from_step_data
from trade_compass_agent.ops.run_store import RunRecord, SqliteRunStore

logger = logging.getLogger(__name__)


class DeliveryRouter:
    """Deliver Job run results to configured channels."""

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self._notification_store = JsonNotificationStore(
            config.data_dir / "notifications.jsonl",
            max_records=config.notifications.max_records,
        )
        self._notifier = NotificationCenter(config, store=self._notification_store)
        self._run_store = SqliteRunStore(config.data_dir / "scheduler.db")

    @property
    def channel_router(self) -> ChannelRouter:
        # Always rebuild so gateway adapter instances (with live subscribers) are used.
        return _build_channel_router()

    def deliver(self, run: RunRecord, delivery: DeliveryConfig) -> None:
        """Deliver a completed/failed run based on its delivery config."""
        if run.status == "skipped":
            return

        if run.ok and delivery.silent_on_success:
            return

        severity = "info" if run.ok else "warning"
        state_label = "完成" if run.ok else "降级" if run.status == "degraded" else "失败"
        title = f"定时任务{state_label}: {run.job_id}"
        content = self._build_rich_content(run)

        if "web_log" in delivery.channels:
            self._notifier.send(Notification(
                channel=f"scheduler:{run.job_id}",
                title=title,
                message=content,
                severity=severity,
            ))

        external_channels = [c for c in delivery.channels if c != "web_log"]
        if external_channels and self.channel_router.adapters:
            msg = ChannelMessage(title=title, content=content, severity=severity)
            for ch in external_channels:
                adapter = self.channel_router.get_adapter(ch)
                if adapter:
                    try:
                        adapter.send_sync(msg)
                    except Exception as exc:
                        logger.warning("Delivery to %s failed: %s", ch, exc)
                else:
                    logger.warning("No adapter found for channel %r, skipping", ch)

    def push_immediate(self, title: str, content: str, severity: str = "info") -> None:
        """Push a message immediately for mid-run alerts."""
        if self.channel_router.adapters:
            msg = ChannelMessage(title=title, content=content, severity=severity)
            self.channel_router.broadcast_sync(msg)

    def _build_rich_content(self, run: RunRecord) -> str:
        """Extract full analysis text from step data_json when available."""
        try:
            step_runs = self._run_store.step_runs_for(run.id)
        except Exception:
            return run.message or run.error or ""

        artifact_analysis = extract_analysis_from_artifact(run.artifact, run_id=run.id)
        if artifact_analysis:
            return artifact_analysis

        parts: list[str] = []
        for sr in step_runs:
            if sr.step_id == "workflow":
                continue
            if sr.status != "completed" or not sr.data_json:
                continue
            analysis = extract_analysis_from_step_data(sr.data_json)
            if isinstance(analysis, str) and len(analysis) > 50:
                parts.append(analysis)

        if parts:
            return "\n\n---\n\n".join(parts)
        return run.message or run.error or ""


def _build_channel_router() -> ChannelRouter:
    router = ChannelRouter()

    # Reuse gateway adapters (they carry subscriber state for push)
    from trade_compass_agent.channels.gateway import get_gateway_adapters
    gw_adapters = get_gateway_adapters()
    for adapter in gw_adapters.values():
        router.register(adapter)

    # Webhook push adapters (if not already provided by gateway)
    if os.environ.get("FEISHU_WEBHOOK_URL") and not router.get_adapter("feishu_webhook"):
        from trade_compass_agent.channels.feishu import FeishuWebhookAdapter
        router.register(FeishuWebhookAdapter())

    if os.environ.get("WECOM_WEBHOOK_URL") and not router.get_adapter("wecom_webhook"):
        from trade_compass_agent.channels.wecom import WecomWebhookAdapter
        router.register(WecomWebhookAdapter())

    if os.environ.get("WEBHOOK_NOTIFICATION_URL"):
        from trade_compass_agent.channels.webhook import WebhookAdapter
        router.register(WebhookAdapter())

    # Fallback: create bot adapters only if gateway didn't provide them
    if not router.get_adapter("feishu") and os.environ.get("FEISHU_APP_ID"):
        from trade_compass_agent.channels.feishu import FeishuBotAdapter
        router.register(FeishuBotAdapter())

    if not router.get_adapter("wecom") and os.environ.get("WECOM_BOT_ID"):
        from trade_compass_agent.channels.wecom import WecomBotAdapter
        router.register(WecomBotAdapter())

    return router
