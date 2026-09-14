"""Failures that remained after the first source recovery pass."""
from datetime import datetime
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import requests

from trade_compass_agent.data import providers
from trade_compass_agent.data.providers import AkshareProvider, ChainProvider, ProviderError, SinaMinuteProvider
from trade_compass_agent.domain import Bar


def response(*, text="", payload=None):
    result = MagicMock()
    result.text = text
    result.json.return_value = payload
    return result


@pytest.mark.parametrize("symbol,market", [("161725", "0"), ("510300", "1"), ("159915", "0"), ("600519", "1")])
@pytest.mark.parametrize("timeframe", ["1m", "5m"])
def test_minute_primary_fetches_one_symbol_without_full_market_lookup(monkeypatch, symbol, market, timeframe):
    provider = AkshareProvider.__new__(AkshareProvider)
    provider.timeout = 1.5
    provider.ak = MagicMock()
    key = "trends" if timeframe == "1m" else "klines"
    row = "2026-09-10 15:00:00,4.618,4.617,4.619,4.617,116462,53777488.5"
    row += ",4.618" if timeframe == "1m" else ",0.1,-0.02,-0.001,0.5"
    get = MagicMock(return_value=response(payload={"rc": 0, "data": {key: [row]}}))
    monkeypatch.setattr(requests, "get", get)
    monkeypatch.setattr(providers, "rate_limit_domain", lambda url: None)
    bars = provider.get_bars(symbol, timeframe, 5)
    assert len(bars) == 1
    assert bars[0].symbol == symbol
    assert bars[0].close == 4.617
    assert bars[0].adjusted is False
    assert get.call_count == 1
    assert get.call_args.kwargs["params"]["secid"] == f"{market}.{symbol}"
    assert get.call_args.kwargs["timeout"] == 1.5
    assert "clist" not in get.call_args.args[0]
    assert provider.ak.method_calls == []


@pytest.mark.parametrize("symbol,expected", [("510300", "sh510300"), ("159915", "sz159915"), ("161725", "sz161725"), ("sh000001", "sh000001")])
def test_sina_minute_supports_etfs_and_preserves_raw_prices_and_timestamp(monkeypatch, symbol, expected):
    rows = [{"day": "2026-09-10 15:00:00", "open": "4.618", "high": "4.619", "low": "4.617", "close": "4.617", "volume": "11646200", "amount": "53777488.5895"}]
    get = MagicMock(return_value=response(text="/* public response */\n=(" + json.dumps(rows) + ");"))
    monkeypatch.setattr(requests, "get", get)
    bars = SinaMinuteProvider(timeout=1.5).get_bars(symbol, "5m", 5)
    assert bars[0].symbol == symbol
    assert bars[0].timestamp == datetime(2026, 9, 10, 15)
    assert bars[0].adjusted is False
    assert bars[0].volume == 11646200
    assert bars[0].amount == 53777488.5895
    assert get.call_count == 1
    assert get.call_args.kwargs["params"]["symbol"] == expected
    assert get.call_args.kwargs["params"]["datalen"] == "5"
    assert get.call_args.kwargs["timeout"] == 1.5


def test_etf_minute_falls_back_after_primary_disconnect_without_background_retries(monkeypatch):
    monkeypatch.setattr(providers, "_market_now", lambda: datetime(2026, 9, 10, 15, 10))
    primary = AkshareProvider.__new__(AkshareProvider)
    primary.ak = MagicMock()
    primary.timeout = 1
    rows = [{"day": "2026-09-10 15:00:00", "open": "4.618", "high": "4.619", "low": "4.617", "close": "4.617"}]
    get = MagicMock(side_effect=[requests.ConnectionError("remote disconnected"), response(text="=(" + json.dumps(rows) + ");")])
    monkeypatch.setattr(requests, "get", get)
    monkeypatch.setattr(providers, "rate_limit_domain", lambda url: None)
    chain = ChainProvider([primary, SinaMinuteProvider(timeout=1)])
    bars = chain.get_bars("510300", "5m", 1)
    assert bars[0].close == 4.617
    assert chain.last_resolved_provider == "sina"
    assert get.call_count == 2
    assert primary.ak.method_calls == []


def test_network_minute_source_must_pass_same_freshness_check_as_cache(monkeypatch):
    monkeypatch.setattr(providers, "_market_now", lambda: datetime(2026, 9, 10, 10, 5))
    def bar(minute):
        return Bar(symbol="510300", timestamp=datetime(2026, 9, 10, 10, minute), open=1, high=1, low=1, close=1, volume=1)
    stale = SimpleNamespace(name="stale", get_bars=lambda *a, **k: [Bar(symbol="510300", timestamp=datetime(2026, 9, 10, 9, 40), open=1, high=1, low=1, close=1, volume=1)])
    fresh = SimpleNamespace(name="current", get_bars=lambda *a, **k: [bar(0)])
    chain = ChainProvider([stale, fresh])
    assert chain.get_bars("510300", "5m", 1)[0].timestamp.hour == 10
    assert chain.last_resolved_provider == "current"
    with pytest.raises(ProviderError, match="stale minute bars"):
        ChainProvider([stale]).get_bars("510300", "5m", 1)


def test_tushare_bounds_https_and_does_not_overwrite_user_token_file(monkeypatch):
    import sys
    from trade_compass_agent.data.tushare_provider import query_tushare
    import requests
    ts = MagicMock()
    monkeypatch.setitem(sys.modules, "tushare", ts)
    reply = MagicMock(status_code=200)
    reply.json.return_value = {"code": 0, "data": {"fields": ["close"], "items": [[10]]}}
    post = MagicMock(return_value=reply)
    monkeypatch.setattr(requests, "post", post)
    monkeypatch.setattr("trade_compass_agent.data.tushare_provider.rate_limit_domain", lambda _: None)
    assert len(query_tushare("daily", token="project-token", timeout=1.5)) == 1
    assert 0 < post.call_args.kwargs["timeout"] <= 1.5
    assert post.call_args.args[0] == "https://api.tushare.pro"
    assert post.call_args.kwargs["allow_redirects"] is False
    ts.set_token.assert_not_called()


def test_bars_api_reports_data_source_failure_instead_of_generic_500(client, monkeypatch):
    from trade_compass_agent.web import api
    provider = MagicMock()
    provider.get_bars.side_effect = ProviderError("510300: all data providers failed; sina: stale minute bars")
    monkeypatch.setattr(api, "_stack", lambda: SimpleNamespace(provider=provider))
    result = client.get("/api/bars?symbol=510300&timeframe=5m&limit=5")
    assert result.status_code == 503
    assert "stale minute bars" in result.json()["detail"]
