"""Trading calendar — determine if today is a trading day.

Uses Chinese stock market calendar rules:
- Mon-Fri only (no weekends)
- Excludes known Chinese holidays
- Best-effort: checks exchange_calendars if available, else simple heuristic
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

logger = logging.getLogger(__name__)

_KNOWN_HOLIDAYS_2025 = {
    date(2025, 1, 1),  # 元旦
    date(2025, 1, 28), date(2025, 1, 29), date(2025, 1, 30), date(2025, 1, 31),  # 春节
    date(2025, 2, 1), date(2025, 2, 2), date(2025, 2, 3), date(2025, 2, 4),
    date(2025, 4, 4), date(2025, 4, 5), date(2025, 4, 6),  # 清明
    date(2025, 5, 1), date(2025, 5, 2), date(2025, 5, 3), date(2025, 5, 4), date(2025, 5, 5),  # 劳动节
    date(2025, 5, 31), date(2025, 6, 1), date(2025, 6, 2),  # 端午
    date(2025, 10, 1), date(2025, 10, 2), date(2025, 10, 3), date(2025, 10, 4),  # 国庆
    date(2025, 10, 5), date(2025, 10, 6), date(2025, 10, 7), date(2025, 10, 8),
}

_KNOWN_HOLIDAYS_2026 = {
    date(2026, 1, 1), date(2026, 1, 2),  # 元旦
    date(2026, 2, 16), date(2026, 2, 17), date(2026, 2, 18), date(2026, 2, 19),  # 春节
    date(2026, 2, 20), date(2026, 2, 21), date(2026, 2, 22),
    date(2026, 4, 5), date(2026, 4, 6), date(2026, 4, 7),  # 清明
    date(2026, 5, 1), date(2026, 5, 2), date(2026, 5, 3),  # 劳动节
    date(2026, 6, 19), date(2026, 6, 20), date(2026, 6, 21),  # 端午
    date(2026, 10, 1), date(2026, 10, 2), date(2026, 10, 3), date(2026, 10, 4),  # 国庆
    date(2026, 10, 5), date(2026, 10, 6), date(2026, 10, 7), date(2026, 10, 8),
}

_ALL_HOLIDAYS = _KNOWN_HOLIDAYS_2025 | _KNOWN_HOLIDAYS_2026


def is_trading_day(d: date | None = None) -> bool:
    """Check if a given date is an A-share trading day.

    Tries exchange_calendars package first (most accurate), then falls back
    to weekday + holiday list check.
    """
    if d is None:
        d = date.today()

    try:
        return _check_exchange_calendars(d)
    except Exception:
        pass

    if d.weekday() >= 5:
        return False
    if d in _ALL_HOLIDAYS:
        return False
    return True


def next_trading_day(d: date | None = None) -> date:
    """Find the next trading day after the given date."""
    if d is None:
        d = date.today()
    candidate = d + timedelta(days=1)
    for _ in range(30):
        if is_trading_day(candidate):
            return candidate
        candidate += timedelta(days=1)
    return candidate


def prev_trading_day(d: date | None = None) -> date:
    """Find the previous trading day before the given date."""
    if d is None:
        d = date.today()
    candidate = d - timedelta(days=1)
    for _ in range(30):
        if is_trading_day(candidate):
            return candidate
        candidate -= timedelta(days=1)
    return candidate


def _check_exchange_calendars(d: date) -> bool:
    """Use exchange_calendars package for accurate SSE calendar."""
    import exchange_calendars as xcals
    cal = xcals.get_calendar("XSHG")
    import pandas as pd
    ts = pd.Timestamp(d)
    return cal.is_session(ts)
