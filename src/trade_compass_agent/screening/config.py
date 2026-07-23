"""Screening configuration with env-override support.

Uses a dataclass with sensible defaults,
overridable via SCREENING_CFG_* environment variables.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass
class ScreeningConfig:
    """Tunable parameters for the screening engine."""

    # L1 hard filters
    min_market_cap_yi: float = 35.0
    min_avg_amount_wan: float = 3000.0
    exclude_st: bool = True
    boards: list[str] = field(
        default_factory=lambda: ["600", "601", "603", "605", "000", "001", "002", "003", "300", "301"]
    )

    # L2 factor weights (must sum to 1.0)
    w_momentum_short: float = 0.15
    w_momentum_mid: float = 0.20
    w_trend: float = 0.25
    w_volume: float = 0.15
    w_volatility: float = 0.10
    w_relative_strength: float = 0.15

    # L3 resonance bonus
    resonance_bonus: float = 0.10

    # L4 trigger bonuses
    trigger_bonus_macd: float = 0.08
    trigger_bonus_rsi: float = 0.06
    trigger_bonus_bollinger: float = 0.06
    trigger_bonus_volume_breakout: float = 0.08

    # Output
    top_n: int = 30
    trading_days: int = 60

    # Data fetch
    fetch_workers: int = 40
    batch_size: int = 200
    batch_sleep: float = 0.5

    @classmethod
    def from_env(cls) -> "ScreeningConfig":
        """Create config with env overrides. SCREENING_CFG_MIN_MARKET_CAP_YI=50 etc."""
        cfg = cls()
        prefix = "SCREENING_CFG_"
        for key, val in os.environ.items():
            if not key.startswith(prefix):
                continue
            attr = key[len(prefix):].lower()
            if hasattr(cfg, attr):
                current = getattr(cfg, attr)
                try:
                    if isinstance(current, bool):
                        setattr(cfg, attr, val.lower() in ("1", "true", "yes"))
                    elif isinstance(current, float):
                        setattr(cfg, attr, float(val))
                    elif isinstance(current, int):
                        setattr(cfg, attr, int(val))
                except (ValueError, TypeError):
                    pass
        return cfg
