"""Describe observations separately from the time an API/tool returns them."""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from trade_compass_agent.domain import Bar


def bars_metadata(bars: list[Bar], provider: str, timeframe: str) -> dict:
    sources = sorted({bar.source for bar in bars if bar.source})
    sample = bool({provider, *sources} & {"sample", "synthetic"})
    # These adapters normalize lots to shares. Other sources do not establish
    # a common volume unit, so consumers must not infer one from the number.
    volume_sources = set(sources) or {provider}
    volume_unit = "shares" if volume_sources <= {"tencent", "tushare"} else "unknown"
    cached = provider == "cache"
    observations = [bar.timestamp.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
                    if bar.timestamp.tzinfo is None else bar.timestamp for bar in bars]
    as_of = max(observations).isoformat() if observations else None
    adjustments = {bar.adjusted for bar in bars}
    adjusted = next(iter(adjustments)) if len(adjustments) == 1 else None
    retrieved_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return {
        "source": provider, "sources": sources, "timeframe": timeframe,
        "timestamp": retrieved_at,  # Compatibility: response generation time only.
        "retrieved_at": retrieved_at, "data_as_of": as_of,
        "origin_fetched_at": None,  # Providers do not expose this reliably.
        "timezone": "Asia/Shanghai", "adjusted": adjusted,
        "adjustment": "mixed" if len(adjustments) > 1 else (
            "adjusted" if adjusted else "unadjusted" if adjusted is False else "unknown"),
        "is_sample": sample, "is_cached": cached,
        "volume_unit": volume_unit,
        "freshness": "sample" if sample else "cached" if cached else "historical" if bars else "unknown",
    }
