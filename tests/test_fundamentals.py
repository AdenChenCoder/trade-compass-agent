from __future__ import annotations

from unittest.mock import MagicMock, patch

import pandas as pd

from trade_compass_agent.data.fundamentals import (
    AkshareFundamentalsProvider,
    RuleFundamentalsProvider,
    create_fundamentals_provider,
)
from trade_compass_agent.data.providers import SampleProvider


def test_rule_fundamentals_from_bars():
    bars = SampleProvider().get_bars("600519", limit=60)
    snapshot = RuleFundamentalsProvider().get_snapshot("600519", bars=bars)
    assert snapshot.symbol == "600519"
    assert snapshot.provider_name == "rule"
    assert any("52w_high" in note for note in snapshot.notes)


@patch("trade_compass_agent.data.fundamentals.run_with_timeout")
def test_akshare_fundamentals_parses_info(mock_timeout: MagicMock):
    df = pd.DataFrame(
        {
            "item": ["市盈率-动态", "市净率", "净资产收益率"],
            "value": ["25.3", "4.2", "18.5"],
        }
    )
    mock_timeout.return_value = df
    provider = AkshareFundamentalsProvider()
    provider.ak = MagicMock()
    provider.ak.stock_individual_info_em = MagicMock(return_value=df)

    snapshot = provider.get_snapshot("600519", bars=SampleProvider().get_bars("600519", limit=30))
    assert snapshot.pe_ttm == 25.3
    assert snapshot.pb == 4.2
    assert snapshot.roe == 18.5
    assert snapshot.has_real_fundamentals


def test_create_fundamentals_provider_always_has_rule_fallback():
    provider = create_fundamentals_provider(tushare_enabled=False)
    snapshot = provider.get_snapshot("600519", bars=SampleProvider().get_bars("600519", limit=30))
    assert snapshot.symbol == "600519"


