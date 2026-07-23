from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from trade_compass_agent.data.providers import ProviderError, create_market_data_provider
from trade_compass_agent.data.tushare_provider import TushareProvider, to_ts_code


def test_to_ts_code():
    assert to_ts_code("600519") == "600519.SH"
    assert to_ts_code("000001") == "000001.SZ"
    assert to_ts_code("510300") == "510300.SH"
    assert to_ts_code("159915") == "159915.SZ"


def test_tushare_provider_requires_token(monkeypatch):
    monkeypatch.delenv("TUSHARE_TOKEN", raising=False)
    with pytest.raises(ProviderError, match="token missing"):
        TushareProvider(token="")


def test_tushare_provider_daily_bars(monkeypatch):
    monkeypatch.setenv("TUSHARE_TOKEN", "test-token")

    class FakeFrame:
        def __init__(self, rows):
            self._rows = rows

        @property
        def empty(self):
            return not self._rows

        def sort_values(self, _key):
            return self

        def tail(self, _n):
            return self

        def iterrows(self):
            for row in self._rows:
                yield (0, row)

    rows = [
        {
            "trade_date": "20240102",
            "open": 10.0,
            "high": 10.5,
            "low": 9.8,
            "close": 10.2,
            "vol": 1000,
            "amount": 10000,
        },
        {
            "trade_date": "20240103",
            "open": 10.2,
            "high": 10.8,
            "low": 10.0,
            "close": 10.6,
            "vol": 1200,
            "amount": 12000,
        },
    ]
    fake_pro = MagicMock()
    fake_pro.daily.return_value = FakeFrame(rows)

    fake_ts = MagicMock()
    fake_ts.pro_api.return_value = fake_pro

    with patch.dict("sys.modules", {"tushare": fake_ts}):
        provider = TushareProvider(token="test-token")
        bars = provider.get_bars("600519", limit=5)

    assert len(bars) == 2
    assert bars[-1].close == 10.6
    assert bars[-1].timestamp == datetime(2024, 1, 3, 15, 0, 0)
    fake_pro.daily.assert_called_once()
    call_kwargs = fake_pro.daily.call_args.kwargs
    assert call_kwargs["ts_code"] == "600519.SH"


def test_tushare_minute_raises(monkeypatch):
    monkeypatch.setenv("TUSHARE_TOKEN", "test-token")
    with patch.dict("sys.modules", {"tushare": MagicMock()}):
        provider = TushareProvider(token="test-token")
        with pytest.raises(ProviderError, match="minute"):
            provider.get_bars("600519", timeframe="5m", limit=5)


def test_auto_chain_skips_tushare_without_token(monkeypatch):
    monkeypatch.delenv("TUSHARE_TOKEN", raising=False)
    from trade_compass_agent.config import DataConfig

    chain = create_market_data_provider("auto", data=DataConfig(tushare_enabled=True))
    names = [p.name for p in chain.providers]
    assert "tushare" not in names
    assert "akshare" in names


def test_auto_chain_includes_tushare_when_enabled(monkeypatch):
    monkeypatch.setenv("TUSHARE_TOKEN", "test-token")
    from trade_compass_agent.config import DataConfig

    with patch.dict("sys.modules", {"tushare": MagicMock()}):
        chain = create_market_data_provider("auto", data=DataConfig(tushare_enabled=True))
    names = [p.name for p in chain.providers]
    assert names[0] == "tushare"
