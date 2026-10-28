"""Calendar dates for dated memory evidence, including legacy naive records."""

from datetime import date, datetime
from zoneinfo import ZoneInfo


def record_day(timestamp: str, timezone_name: str) -> date:
    stamp = datetime.fromisoformat(timestamp)
    if stamp.tzinfo is not None:
        stamp = stamp.astimezone(ZoneInfo(timezone_name))
    return stamp.date()
