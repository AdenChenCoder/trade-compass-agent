from __future__ import annotations

from dataclasses import dataclass

from trade_compass_agent.config import AppConfig, load_app_config
from trade_compass_agent.data import (
    FundamentalsProvider,
    MarketDataProvider,
    create_cninfo_provider,
    create_fundamentals_provider,
    create_market_data_provider,
    create_market_pulse_provider,
)
from trade_compass_agent.data.cninfo import CninfoProvider


@dataclass(frozen=True)
class MarketStack:
    config: AppConfig
    provider: MarketDataProvider
    market_pulse_provider: object
    cninfo_provider: CninfoProvider
    fundamentals_provider: FundamentalsProvider

    @classmethod
    def from_config(cls, config: AppConfig | None = None) -> MarketStack:
        app_config = config or load_app_config()
        provider = create_market_data_provider(
            app_config.data_provider,
            cache_dir=app_config.data_dir / "market_cache",
            data=app_config.data,
        )
        pulse_name = app_config.data_provider
        cninfo_name = app_config.data_provider if app_config.data_provider == "sample" else "auto"
        return cls(
            config=app_config,
            provider=provider,
            market_pulse_provider=create_market_pulse_provider(pulse_name),
            cninfo_provider=create_cninfo_provider(cninfo_name),
            fundamentals_provider=create_fundamentals_provider(
                tushare_enabled=app_config.data.tushare_enabled,
                tushare_token_env=app_config.data.tushare_token_env,
            ),
        )
