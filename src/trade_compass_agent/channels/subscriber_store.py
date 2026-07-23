"""Persist messaging channel subscriber IDs across process restarts."""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_DEFAULT_PATH = Path("channel_subscribers.json")


def _default_payload() -> dict[str, list[str]]:
    return {"feishu": [], "weixin": [], "wecom": []}


def load_channel_subscribers(path: Path) -> dict[str, set[str]]:
    if not path.exists():
        return {k: set(v) for k, v in _default_payload().items()}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Failed to load channel subscribers from %s: %s", path, exc)
        return {k: set(v) for k, v in _default_payload().items()}
    result: dict[str, set[str]] = {}
    for key, values in _default_payload().items():
        items = raw.get(key, values) if isinstance(raw, dict) else values
        if isinstance(items, list):
            result[key] = {str(item).strip() for item in items if str(item).strip()}
        else:
            result[key] = set()
    return result


def save_channel_subscribers(path: Path, data: dict[str, set[str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {key: sorted(values) for key, values in data.items()}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    path.chmod(0o600)
