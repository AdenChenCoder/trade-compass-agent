"""Free-source consumers, upstream faults, and persistent cache contracts."""
from dataclasses import replace
from datetime import datetime
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import requests

from trade_compass_agent.data import providers
from trade_compass_agent.data.providers import BulkDailyBarProvider, ChainProvider, LocalBarCacheProvider, ProviderError
from trade_compass_agent.data.tencent_provider import TencentProvider
from trade_compass_agent.domain import Bar
from trade_compass_agent.runtime.tools import search


@pytest.fixture(autouse=True)
def source_state(monkeypatch):
    monkeypatch.setattr(TencentProvider, "_next_request", 0.0)
    monkeypatch.setattr(TencentProvider, "_cooldown_until", 0.0)
    monkeypatch.setattr(providers, "_market_now", lambda: datetime(2026, 9, 11, 16))


def response(payload, status=200):
    r = requests.Response()
    r.status_code = status
    r.url = "https://proxy.finance.qq.com/"
    r._content = json.dumps(payload).encode()
    return r


def bar(day=11, *, source="tencent", adjusted=True):
    return Bar("600519", datetime(2026, 9, day, 15), 10, 12, 9, 11, 2000,
               amount=22000, adjusted=adjusted, turnover_pct=0.5, source=source)


@pytest.mark.parametrize("symbol,code", [("600519", "sh600519"), ("000001", "sz000001"), ("510300", "sh510300"), ("161725", "sz161725"), ("sh000001", "sh000001")])
@pytest.mark.parametrize("timeframe", ["1d", "1m", "5m"])
def test_tencent_real_fields_identity_and_units(monkeypatch, symbol, code, timeframe):
    daily = timeframe == "1d"
    row = ["2026-09-11" if daily else "202609111500", "10", "11", "12", "9", "20", {}, "0.5"]
    if daily:
        row.append("2.2")
    key = "qfqday" if daily else "m" + timeframe[:-1]
    get = MagicMock(return_value=response({"code": 0, "data": {code: {key: [row]}}}))
    monkeypatch.setattr(requests, "get", get)
    result = TencentProvider().get_bars(symbol, timeframe, 1)[0]
    assert result.symbol == symbol
    assert result.timestamp == datetime(2026, 9, 11, 15)
    assert result.volume == 2000
    assert result.amount == (22000 if daily else None)
    assert result.adjusted is (daily and symbol != "sh000001")
    assert result.source == "tencent"
    assert get.call_count == 1
    assert get.call_args.kwargs["params"]["param"].startswith(code + ",")


def test_tencent_unadjusted_response_is_not_labelled_adjusted(monkeypatch):
    monkeypatch.setattr(requests, "get", lambda *a, **k: response({"code": 0, "data": {"sz161725": {
        "day": [["2026-09-11", ".54", ".533", ".55", ".53", "20"]],
    }}}))
    result = TencentProvider().get_bars("161725", limit=1)[0]
    assert not result.adjusted
    assert result.amount is None


@pytest.mark.parametrize("payload", [
    {"code": -1, "msg": "invalid symbol"}, {"code": 0, "data": {}},
    {"code": 0, "data": {"sh600519": {"qfqday": [["2026-09-11", "nan", "11", "12", "9", "20"]]}}},
    {"code": 0, "data": {"sh600519": {"qfqday": [["2026-09-11", "10", "13", "12", "9", "20"]]}}},
])
def test_tencent_http_200_invalid_data_fails(monkeypatch, payload):
    monkeypatch.setattr(requests, "get", lambda *a, **k: response(payload))
    with pytest.raises(ProviderError):
        TencentProvider().get_bars("600519", limit=1)


def test_tencent_stale_data_falls_back(monkeypatch):
    monkeypatch.setattr(requests, "get", lambda *a, **k: response({"code": 0, "data": {"sh600519": {
        "qfqday": [["2026-09-10", "10", "11", "12", "9", "20"]],
    }}}))
    chain = ChainProvider([TencentProvider(), SimpleNamespace(name="backup", get_bars=lambda *a, **k: [bar()])])
    assert chain.get_bars("600519", limit=1)[0].timestamp.day == 11
    assert chain.last_resolved_provider == "backup"


def test_tencent_rate_limit_does_not_trigger_request_storm_and_recovers(monkeypatch):
    get = MagicMock(return_value=response({}, 429))
    monkeypatch.setattr(requests, "get", get)
    provider = TencentProvider()
    for _ in range(3):
        with pytest.raises(ProviderError):
            provider.get_bars("600519", limit=1)
    assert get.call_count == 1
    monkeypatch.setattr(TencentProvider, "_cooldown_until", 0.0)
    get.return_value = response({"code": 0, "data": {"sh600519": {"qfqday": [["2026-09-11", "10", "11", "12", "9", "20"]]}}})
    assert provider.get_bars("600519", limit=1)
    assert get.call_count == 2


def test_tencent_queue_is_bounded(monkeypatch):
    import threading
    monkeypatch.setattr(TencentProvider, "_slots", threading.BoundedSemaphore(0))
    get = MagicMock()
    monkeypatch.setattr(requests, "get", get)
    with pytest.raises(ProviderError, match="queue timeout"):
        TencentProvider(timeout=0.01).get_bars("600519")
    get.assert_not_called()


def test_default_market_chain_prefers_free_sources(monkeypatch, tmp_path):
    monkeypatch.setenv("TUSHARE_TOKEN", "saved-token")
    chain = providers.create_market_data_provider(cache_dir=tmp_path)
    names = [p.name for p in chain.providers]
    assert names[:3] == ["cache", "sina", "tencent"]
    assert "tushare" not in names


def test_cache_preserves_source_turnover_and_survives_restart(tmp_path):
    LocalBarCacheProvider(tmp_path).write_bars("600519", "1d", [bar()])
    result = LocalBarCacheProvider(tmp_path).get_bars("600519", limit=1)[0]
    assert result == bar()


def test_short_history_is_reused_only_after_equally_large_upstream_request(tmp_path):
    cache = LocalBarCacheProvider(tmp_path)
    cache.write_bars("600519", "1d", [bar()], requested_limit=1)
    with pytest.raises(ProviderError, match="need 60"):
        cache.get_bars("600519", limit=60)
    cache.write_bars("600519", "1d", [bar()], requested_limit=60)
    assert LocalBarCacheProvider(tmp_path).get_bars("600519", limit=60) == [bar()]
    with pytest.raises(ProviderError, match="need 120"):
        cache.get_bars("600519", limit=120)


def test_bars_api_preserves_upstream_identity_and_units(client, monkeypatch):
    from trade_compass_agent.web import api
    stack = SimpleNamespace(provider=SimpleNamespace(name="auto", get_bars=lambda *a, **k: [bar()]))
    monkeypatch.setattr(api, "_stack", lambda: stack)
    result = client.get("/api/bars?symbol=600519&timeframe=1d&limit=1")
    assert result.status_code == 200
    data = result.json()["bars"][0]
    assert data["source"] == "tencent"
    assert data["volume"] == 2000
    assert data["amount"] == 22000


def test_corrupt_derived_cache_keeps_recovery_copy_and_can_refresh(tmp_path):
    path = tmp_path / "1d" / "600519.jsonl"
    path.parent.mkdir()
    broken = '{"symbol":"600519"}{"truncated":'
    path.write_text(broken)
    cache = LocalBarCacheProvider(tmp_path)
    with pytest.raises(ProviderError):
        cache.get_bars("600519", limit=1)
    cache.write_bars("600519", "1d", [bar()])
    assert cache.get_bars("600519", limit=1) == [bar()]
    assert next(path.parent.glob("*.corrupt-*")).read_text() == broken


def test_cache_does_not_mix_sources_or_adjustment_bases(tmp_path):
    cache = LocalBarCacheProvider(tmp_path)
    cache.write_bars("600519", "1d", [bar(9, source=None), bar(10, source=None)])
    cache.write_bars("600519", "1d", [bar(10), bar(11)])
    assert len(cache._path("600519", "1d").read_text().splitlines()) == 2
    rebased = replace(bar(11), open=5, high=6, low=4.5, close=5.5)
    cache.write_bars("600519", "1d", [rebased])
    assert cache.get_bars("600519", limit=1) == [rebased]
    assert len(cache._path("600519", "1d").read_text().splitlines()) == 1


def test_screening_uses_last_closed_day_during_trading_without_network(monkeypatch, tmp_path):
    monkeypatch.setattr(providers, "_market_now", lambda: datetime(2026, 9, 14, 10))
    cache = LocalBarCacheProvider(tmp_path)
    cache.write_bars("600519", "1d", [bar(10), bar(11), bar(14)])
    provider = BulkDailyBarProvider(cache_dir=tmp_path)
    provider._network = MagicMock()
    assert provider.get_bars("600519", limit=2) == [bar(10), bar(11)]
    provider._network.get_bars.assert_not_called()


def test_screening_network_also_excludes_unclosed_daily_bar(monkeypatch):
    monkeypatch.setattr(providers, "_market_now", lambda: datetime(2026, 9, 14, 10))
    provider = BulkDailyBarProvider()
    provider._network = MagicMock()
    provider._network.get_bars.return_value = [bar(10), bar(11), bar(14)]
    assert provider.get_bars("600519", limit=2) == [bar(10), bar(11)]
    assert provider._network.get_bars.call_args.kwargs["limit"] == 3


def test_default_search_does_not_use_paid_provider_even_with_saved_key(monkeypatch):
    monkeypatch.delenv("WEB_SEARCH_PROVIDER", raising=False)
    monkeypatch.setenv("TAVILY_API_KEY", "saved-key")
    paid = MagicMock()
    monkeypatch.setattr(search, "_web_search_tavily", paid)
    monkeypatch.setattr(search, "_web_search_ddg", lambda **k: '{"provider":"brave","count":1}')
    assert json.loads(search.tool_web_search(query="公告"))["provider"] == "brave"
    paid.assert_not_called()


def test_disabled_ddgs_engine_is_never_mislabelled_as_its_auto_fallback(monkeypatch):
    import sys
    instance = MagicMock()
    instance.__enter__.return_value = instance
    instance.text.return_value = [{"title": "公告", "href": "https://www.sse.com.cn/", "body": "信息"}]
    monkeypatch.setitem(sys.modules, "ddgs", SimpleNamespace(DDGS=MagicMock(return_value=instance)))
    monkeypatch.setitem(sys.modules, "ddgs.engines", SimpleNamespace(ENGINES={"text": {"yahoo": object}}))
    result = json.loads(search._web_search_ddg(query="公告", limit=1))
    assert result["provider"] == "yahoo"
    assert instance.text.call_args.kwargs["backend"] == "yahoo"
    assert instance.text.call_args.kwargs["region"] == "cn-zh"


@pytest.mark.parametrize("now", [datetime(2026, 9, 12, 8), datetime(2026, 9, 14, 8)])
@pytest.mark.parametrize("timeframe", ["1d", "1m"])
def test_off_session_cache_requires_completed_previous_session(tmp_path, monkeypatch, now, timeframe):
    import os
    from zoneinfo import ZoneInfo
    monkeypatch.setattr(providers, "_market_now", lambda: now)
    cache = providers.LocalBarCacheProvider(tmp_path)
    old = Bar(symbol="161725", timestamp=datetime(2026, 9, 11, 11, 10), open=1, high=1, low=1, close=1, volume=100)
    cache.write_bars("161725", timeframe, [old])
    stamp = old.timestamp.replace(tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()
    os.utime(cache._path("161725", timeframe), (stamp, stamp))
    with pytest.raises(ProviderError, match="stale cache"):
        cache.get_bars("161725", timeframe, 1)
    cache.write_bars("161725", timeframe, [replace(old, timestamp=datetime(2026, 9, 11, 15))])
    assert cache.get_bars("161725", timeframe, 1)[-1].timestamp.hour == 15


def test_screening_rejects_previous_session_unfinished_snapshot(tmp_path, monkeypatch):
    import os
    from zoneinfo import ZoneInfo
    monkeypatch.setattr(providers, "_market_now", lambda: datetime(2026, 9, 12, 8))
    cache = providers.LocalBarCacheProvider(tmp_path, closed_daily_only=True)
    old = Bar(symbol="161725", timestamp=datetime(2026, 9, 11, 15), open=1, high=1, low=1, close=1, volume=100)
    cache.write_bars("161725", "1d", [old])
    stamp = datetime(2026, 9, 11, 11, 10, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()
    os.utime(cache._path("161725", "1d"), (stamp, stamp))
    with pytest.raises(ProviderError, match="stale cache"):
        cache.get_bars("161725", "1d", 1)


def test_tavily_requires_explicit_provider_and_valid_key(monkeypatch):
    monkeypatch.setenv("WEB_SEARCH_PROVIDER", "tavily")
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    assert "error" in json.loads(search.tool_web_search(query="公告"))
