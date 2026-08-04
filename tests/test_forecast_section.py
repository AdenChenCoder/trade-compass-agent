from __future__ import annotations

import json

from trade_compass_agent.runtime.loop import _compact_forecast_summary


def test_forecast_section_preserves_quality_metadata() -> None:
    section = _compact_forecast_summary(
        json.dumps(
            {
                "symbol": "600519",
                "model": "NeoQuasar/Kronos-small",
                "horizon_bars": 3,
                "forecast_summary": {"change_pct": 1.2, "direction": "up"},
                "forecast_bars": [
                    {
                        "timestamp": "2026-08-05",
                        "open": 1,
                        "high": 2,
                        "low": 1,
                        "close": 2,
                        "volume": 3,
                    }
                ],
                "confidence_band": {"upper": [2.1], "lower": [0.9]},
                "quality_status": "experimental",
                "parameters": {
                    "horizon": 3,
                    "model_size": "small",
                    "sample_count": 5,
                    "lookback": 120,
                },
            }
        )
    )

    assert section is not None
    assert section.forecast_data is not None
    assert section.forecast_data["symbol"] == "600519"
    assert section.forecast_data["model"] == "NeoQuasar/Kronos-small"
    assert section.forecast_data["quality_status"] == "experimental"
    assert section.forecast_data["parameters"]["horizon"] == 3
