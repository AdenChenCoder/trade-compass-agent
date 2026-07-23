import threading
import time

import pytest

from trade_compass_agent.data.providers import (
    BulkDailyBarProvider,
    ChainProvider,
    LocalBarCacheProvider,
    ProviderError,
    SampleProvider,
    to_sina_code,
    to_baostock_code,
)
from trade_compass_agent.data.network import run_with_timeout


class _FailingProvider:
    name = "failing"

    def get_instrument(self, symbol: str):
        raise ProviderError("nope")

    def get_bars(self, symbol: str, timeframe: str = "1d", limit: int = 120):
        raise ProviderError("nope")


class _FixedProvider:
    name = "fixed"

    def get_instrument(self, symbol: str):
        from trade_compass_agent.domain import Instrument, InstrumentKind

        return Instrument(symbol=symbol, name=symbol, kind=InstrumentKind.STOCK)

    def get_bars(self, symbol: str, timeframe: str = "1d", limit: int = 120):
        return SampleProvider().get_bars(symbol, timeframe=timeframe, limit=3)


def test_run_with_timeout_uses_daemon_worker():
    worker_is_daemon = run_with_timeout(
        lambda: threading.current_thread().daemon,
        timeout=0.5,
        description="daemon-check",
    )

    assert worker_is_daemon is True


def test_to_baostock_code():
    assert to_baostock_code("600519") == "sh.600519"
    assert to_baostock_code("000001") == "sz.000001"
    assert to_baostock_code("510300") == "sh.510300"
    assert to_baostock_code("159915") == "sz.159915"


def test_to_sina_code():
    assert to_sina_code("600519") == "sh600519"
    assert to_sina_code("000001") == "sz000001"
    assert to_sina_code("510300") == "sh510300"


def test_chain_provider_raises_when_all_fail():
    """ChainProvider ignores sample providers and raises when all real providers fail."""
    chain = ChainProvider([_FailingProvider(), SampleProvider()])
    with pytest.raises(ProviderError):
        chain.get_bars("600519", limit=5)


def test_chain_provider_uses_secondary_when_primary_fails():
    chain = ChainProvider([_FailingProvider(), _FixedProvider()])
    bars = chain.get_bars("600519", limit=3)
    assert len(bars) == 3
    assert chain.last_resolved_provider == "fixed"


def test_chain_provider_enforces_total_timeout_budget():
    class _SlowProvider:
        name = "slow"

        def get_instrument(self, symbol: str):
            return _FixedProvider().get_instrument(symbol)

        def get_bars(self, symbol: str, timeframe: str = "1d", limit: int = 120):
            time.sleep(0.06)
            raise ProviderError("slow provider failed")

    secondary = _FixedProvider()
    chain = ChainProvider([_SlowProvider(), secondary], total_timeout=0.05)

    started = time.monotonic()
    with pytest.raises(ProviderError, match="timeout budget"):
        chain.get_bars("600519", limit=3)

    assert time.monotonic() - started < 0.12
    assert chain.last_resolved_provider is None


def test_sample_provider_generates_minute_bars():
    bars = SampleProvider().get_bars("600519", timeframe="5m", limit=12)
    assert len(bars) == 12
    assert bars[-1].timestamp > bars[0].timestamp


def test_local_bar_cache_provider_round_trips(tmp_path):
    cache = LocalBarCacheProvider(tmp_path)
    source = SampleProvider().get_bars("600519", timeframe="5m", limit=12)
    cache.write_bars("600519", "5m", source)
    loaded = cache.get_bars("600519", timeframe="5m", limit=5)
    assert len(loaded) == 5
    assert loaded[-1].close == source[-1].close


def test_chain_provider_uses_cache_before_sample_for_minutes(tmp_path):
    cache = LocalBarCacheProvider(tmp_path)
    cache.write_bars("600519", "5m", SampleProvider().get_bars("600519", timeframe="5m", limit=8))
    chain = ChainProvider([_FailingProvider(), cache])
    bars = chain.get_bars("600519", timeframe="5m", limit=3)
    assert len(bars) == 3
    assert chain.last_resolved_provider == "cache"


def test_bulk_daily_provider_uses_cache_before_network(tmp_path):
    cache = LocalBarCacheProvider(tmp_path)
    source = SampleProvider().get_bars("600519", timeframe="1d", limit=10)
    cache.write_bars("600519", "1d", source)

    class _SlowNetwork:
        name = "slow"

        def get_instrument(self, symbol: str):
            return _FixedProvider().get_instrument(symbol)

        def get_bars(self, symbol: str, timeframe: str = "1d", limit: int = 120):
            raise ProviderError("network should not be called when cache hit")

    provider = BulkDailyBarProvider(cache_dir=tmp_path, request_timeout=1.0)
    provider._network = ChainProvider([_SlowNetwork()])
    bars = provider.get_bars("600519", timeframe="1d", limit=5)
    assert len(bars) == 5
    assert bars[-1].close == source[-1].close


def test_bulk_daily_provider_writes_cache_on_network_fetch(tmp_path):
    provider = BulkDailyBarProvider(cache_dir=tmp_path, request_timeout=1.0)
    provider._network = ChainProvider([_FixedProvider()])
    bars = provider.get_bars("600519", timeframe="1d", limit=3)
    assert len(bars) == 3
    cached = LocalBarCacheProvider(tmp_path).get_bars("600519", timeframe="1d", limit=3)
    assert cached[-1].close == bars[-1].close
