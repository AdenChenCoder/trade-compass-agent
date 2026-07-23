from __future__ import annotations

from dataclasses import dataclass, field

from trade_compass_agent.domain import Bar


@dataclass(frozen=True)
class DataQualityReport:
    ok: bool
    warnings: list[str] = field(default_factory=list)


class DataQualityLayer:
    def check_bars(self, bars: list[Bar]) -> DataQualityReport:
        warnings: list[str] = []
        if not bars:
            return DataQualityReport(ok=False, warnings=["no bars returned"])
        timestamps = [bar.timestamp for bar in bars]
        if timestamps != sorted(timestamps):
            warnings.append("bars are not sorted by timestamp")
        for bar in bars:
            if bar.high < max(bar.open, bar.close) or bar.low > min(bar.open, bar.close):
                warnings.append(f"invalid OHLC range at {bar.timestamp.isoformat()}")
                break
            if bar.volume < 0:
                warnings.append(f"negative volume at {bar.timestamp.isoformat()}")
                break
        return DataQualityReport(ok=not warnings, warnings=warnings)
