from __future__ import annotations

import json
from collections.abc import Callable

from trade_compass_agent.config import AppConfig
from trade_compass_agent.runtime.market_stack import MarketStack
from trade_compass_agent.runtime.specialists.asset_runner import run_asset_specialist
from trade_compass_agent.runtime.specialists.assets import load_specialist_profiles
from trade_compass_agent.runtime.types import TurnEvent


def run_specialist(
    stack: MarketStack,
    name: str,
    task: str,
    *,
    config: AppConfig | None = None,
    on_event: Callable[[TurnEvent], None] | None = None,
) -> str:
    profiles = load_specialist_profiles()
    profile = profiles.get(name)
    if profile is None:
        return json.dumps(
            {"error": f"unknown specialist: {name}", "available": sorted(profiles)},
            ensure_ascii=False,
        )
    return run_asset_specialist(
        stack,
        profile,
        task,
        config=config,
        on_event=on_event,
    )
