from datetime import datetime
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import requests

from trade_compass_agent.config import DataConfig
from trade_compass_agent.data import providers
from trade_compass_agent.data.tushare_provider import TushareProvider
from trade_compass_agent.domain import Bar, InstrumentKind


def response(symbol="561560.SH", **overrides):
    row = dict(ts_code=symbol, trade_date="20260911", open=1.281,
               high=1.287, low=1.265, close=1.281, vol=534864, amount=68364.82)
    row.update(overrides)
    result = requests.Response()
    result.status_code = 200
    result._content = json.dumps({"code": 0, "data": {
        "fields": list(row), "items": [list(row.values())],
    }}).encode()
    return result


@pytest.fixture
def http(monkeypatch):
    from trade_compass_agent.data import network
    monkeypatch.setattr(network._rate_limiter, "wait", lambda _: None)
    post = MagicMock(return_value=response())
    monkeypatch.setattr(requests, "post", post)
    return post


def test_held_etf_reaches_fund_api_with_correct_units_at_web_and_agent(client, monkeypatch, http):
    from trade_compass_agent.runtime.tools.market import tool_get_bars
    from trade_compass_agent.web import api
    provider = TushareProvider(token="test-token")
    stack = SimpleNamespace(provider=provider)
    monkeypatch.setattr(api, "_stack", lambda: stack)
    result = client.get("/api/bars?symbol=561560&timeframe=1d&limit=1")
    assert result.status_code == 200
    web_bar = result.json()["bars"][0]
    agent_bar = json.loads(tool_get_bars(stack, symbol="561560", limit=1))["bars"][0]
    for bar in (web_bar, agent_bar):
        assert bar["volume"] == 53_486_400
        assert bar["amount"] == pytest.approx(68_364_820)
        assert bar["close"] == 1.281
        assert bar["adjusted"] is False
        assert bar["source"] == "tushare"
    assert provider.get_instrument("561560").kind == InstrumentKind.ETF
    for call in http.call_args_list:
        assert call.args[0] == "https://api.tushare.pro"
        assert call.kwargs["json"]["api_name"] == "fund_daily"


@pytest.mark.parametrize("symbol,api_name", [
    ("600519", "daily"), ("000001", "daily"),
    ("sh000001", "index_daily"), ("sz399001", "index_daily"),
    ("161725", "fund_daily"), ("510300", "fund_daily"), ("561560", "fund_daily"),
])
def test_daily_instrument_identity(symbol, api_name, http):
    from trade_compass_agent.data.tushare_provider import to_ts_code
    http.return_value = response(to_ts_code(symbol))
    assert TushareProvider(token="test-token").get_bars(symbol, limit=1)[0].symbol == symbol
    assert http.call_args.kwargs["json"]["api_name"] == api_name


@pytest.mark.parametrize("changes", [
    {"close": float("nan")}, {"vol": -1}, {"amount": -1}, {"low": 2},
    {"trade_date": "invalid"}, {"ts_code": "600519.SH"},
    {"trade_date": "20260912"}, {"trade_date": "20990101"},
])
def test_invalid_or_wrong_security_data_cannot_enter_cache(changes, http):
    http.return_value = response(**changes)
    with pytest.raises(providers.ProviderError):
        TushareProvider(token="test-token").get_bars("561560", limit=1)


def test_missing_amount_is_not_fabricated_as_zero(http):
    http.return_value = response(amount=None)
    assert TushareProvider(token="test-token").get_bars("561560", limit=1)[0].amount is None


def test_permission_error_remains_a_failure_and_redacts_token(http):
    http.return_value._content = json.dumps({"code": 40203, "msg": "denied test-token"}).encode()
    with pytest.raises(providers.ProviderError, match="40203") as exc:
        TushareProvider(token="test-token").get_bars("561560", limit=1)
    assert "test-token" not in str(exc.value)


def test_daily_window_can_cover_requested_trading_days(http):
    TushareProvider(token="test-token").get_bars("561560", limit=120)
    params = http.call_args.kwargs["json"]["params"]
    start = datetime.strptime(params["start_date"], "%Y%m%d")
    end = datetime.strptime(params["end_date"], "%Y%m%d")
    assert (end - start).days >= 240


def test_weekend_and_intraday_requests_stop_at_last_closed_trade_date(monkeypatch, http):
    from trade_compass_agent.data import tushare_provider
    for now in (datetime(2026, 9, 12, 23), datetime(2026, 9, 14, 10)):
        monkeypatch.setattr(tushare_provider, "_market_now", lambda: now)
        TushareProvider(token="test-token").get_bars("561560", limit=1)
        assert http.call_args.kwargs["json"]["params"]["end_date"] == "20260911"


def test_free_failure_reaches_tushare_and_survives_cache_restart(tmp_path, monkeypatch, http):
    monkeypatch.setenv("TUSHARE_TOKEN", "test-token")
    monkeypatch.setattr(providers, "_market_now", lambda: datetime(2026, 9, 12, 16))
    chain = providers.create_market_data_provider("auto", data=DataConfig(tushare_enabled=True), cache_dir=tmp_path)
    for provider in chain.providers:
        if provider.name not in {"cache", "tushare"}:
            monkeypatch.setattr(provider, "get_bars", MagicMock(side_effect=providers.ProviderError("offline")))
    bars = chain.get_bars("561560", limit=1)
    assert chain.last_resolved_provider == "tushare"
    cached = providers.LocalBarCacheProvider(tmp_path).get_bars("561560", limit=1)
    assert cached == bars
    assert cached[0].source == "tushare" and cached[0].adjusted is False


def test_paid_timeout_leaves_time_for_free_fallback(monkeypatch, http):
    clock = [0.0]
    monkeypatch.setattr(providers.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(providers, "_market_now", lambda: datetime(2026, 9, 12, 16))
    def timeout_call(fn, timeout, label):
        if label.startswith("tushare"):
            clock[0] += timeout
            raise TimeoutError("paid source timed out")
        return fn()
    monkeypatch.setattr(providers, "run_with_timeout", timeout_call)
    bars = [Bar("561560", datetime(2026, 9, 11, 15), 1, 1, 1, 1, 100, source="tencent")]
    free = SimpleNamespace(name="tencent", supported_timeframes={"1d"}, get_bars=lambda *a, **k: bars)
    chain = providers.ChainProvider([TushareProvider(token="test-token"), free], total_timeout=8)
    assert chain.get_bars("561560", limit=1) == bars
    assert chain.last_resolved_provider == "tencent"
    assert clock[0] < 8
    http.assert_not_called()


def test_bulk_fallback_obeys_the_same_explicit_option(monkeypatch):
    monkeypatch.setenv("TUSHARE_TOKEN", "test-token")
    for enabled in (False, True):
        bulk = providers.create_bulk_daily_provider(data=DataConfig(tushare_enabled=enabled))
        names = [p.name for p in bulk._network.providers]
        assert ("tushare" in names) is enabled
        if enabled:
            assert names[0] == "tushare"


def test_disabled_paid_option_does_not_call_tushare(monkeypatch, http):
    from trade_compass_agent.domain import Bar
    monkeypatch.setenv("TUSHARE_TOKEN", "test-token")
    monkeypatch.setattr(providers, "_market_now", lambda: datetime(2026, 9, 12, 16))
    chain = providers.create_market_data_provider("auto", data=DataConfig(tushare_enabled=False))
    free = next(p for p in chain.providers if p.name == "tencent")
    bars = [Bar("561560", datetime(2026, 9, 11, 15), 1, 1, 1, 1, 100, source="tencent")]
    monkeypatch.setattr(free, "get_bars", lambda *a, **k: bars)
    assert chain.get_bars("561560", limit=1) == bars
    http.assert_not_called()


def test_stale_tushare_data_is_rejected_by_the_consumer(monkeypatch, http):
    monkeypatch.setattr(providers, "_market_now", lambda: datetime(2026, 9, 12, 16))
    http.return_value = response(trade_date="20260910")
    chain = providers.ChainProvider([TushareProvider(token="test-token")])
    with pytest.raises(providers.ProviderError, match="stale daily"):
        chain.get_bars("561560", limit=1)


def test_fundamentals_prefer_enabled_paid_source_and_use_same_https(http, monkeypatch):
    from trade_compass_agent.data.fundamentals import create_fundamentals_provider
    monkeypatch.setenv("TUSHARE_TOKEN", "test-token")
    chain = create_fundamentals_provider(tushare_enabled=True)
    names = [p.name for p in chain.providers]
    assert names[0] == "tushare"
    row = {"trade_date": "20260911", "pe_ttm": 19.36, "pb": 6.34, "total_mv": 159405405.3}
    http.return_value._content = json.dumps({"code": 0, "data": {"fields": list(row), "items": [list(row.values())]}}).encode()
    for provider in chain.providers[1:]:
        monkeypatch.setattr(provider, "get_snapshot", MagicMock(side_effect=AssertionError("free source called")))
    snapshot = chain.get_snapshot("600519")
    assert snapshot.pe_ttm == 19.36
    assert snapshot.market_cap == pytest.approx(1594054053000)
    assert http.call_args.args[0] == "https://api.tushare.pro"
    for provider in chain.providers[1:]:
        provider.get_snapshot.assert_not_called()


@pytest.mark.parametrize("bulk", [False, True])
@pytest.mark.parametrize("cached_source", [None, "tencent"])
@pytest.mark.parametrize("now", [datetime(2026, 9, 12, 16), datetime(2026, 9, 14, 10)])
def test_enabled_paid_source_replaces_free_cache_and_reuses_paid_cache(tmp_path, monkeypatch, http, bulk, cached_source, now):
    from trade_compass_agent.data import tushare_provider
    monkeypatch.setenv("TUSHARE_TOKEN", "test-token")
    monkeypatch.setattr(providers, "_market_now", lambda: now)
    monkeypatch.setattr(tushare_provider, "_market_now", lambda: now)
    bars = [Bar("561560", datetime(2026, 9, 11, 15), 1, 1, 1, 1, 100, source=cached_source)]
    providers.LocalBarCacheProvider(tmp_path).write_bars("561560", "1d", bars, requested_limit=2)
    factory = providers.create_bulk_daily_provider if bulk else providers.create_market_data_provider
    options = dict(data=DataConfig(tushare_enabled=True), cache_dir=tmp_path)
    chain = factory(**options)
    network = chain._network if bulk else chain
    for provider in network.providers:
        if provider.name not in {"cache", "tushare"}:
            monkeypatch.setattr(provider, "get_bars", MagicMock(return_value=bars))
    result = chain.get_bars("561560", limit=1)
    assert result[0].source == "tushare"
    assert result[0].close == 1.281
    http.assert_called_once()
    for provider in network.providers:
        if provider.name not in {"cache", "tushare"}:
            provider.get_bars.assert_not_called()
    restarted = factory(**options)
    assert restarted.get_bars("561560", limit=1) == result
    http.assert_called_once()


@pytest.mark.parametrize("bulk", [False, True])
@pytest.mark.parametrize("failure", ["permission", "stale"])
def test_paid_failure_reaches_fresh_free_cache(tmp_path, monkeypatch, http, bulk, failure):
    monkeypatch.setenv("TUSHARE_TOKEN", "test-token")
    monkeypatch.setattr(providers, "_market_now", lambda: datetime(2026, 9, 12, 16))
    bars = [Bar("561560", datetime(2026, 9, 11, 15), 1, 1, 1, 1, 100, source="tencent")]
    providers.LocalBarCacheProvider(tmp_path).write_bars("561560", "1d", bars, requested_limit=2)
    if failure == "permission":
        http.return_value._content = json.dumps({"code": 40203, "msg": "denied"}).encode()
    else:
        http.return_value = response(trade_date="20260910")
    factory = providers.create_bulk_daily_provider if bulk else providers.create_market_data_provider
    chain = factory(data=DataConfig(tushare_enabled=True), cache_dir=tmp_path)
    assert chain.get_bars("561560", limit=1) == bars
    http.assert_called_once()
    # A fallback cache must not prevent another attempt after recovery.
    http.return_value = response()
    assert factory(data=DataConfig(tushare_enabled=True), cache_dir=tmp_path).get_bars("561560", limit=1)[0].source == "tushare"
    assert http.call_count == 2


def test_paid_daily_option_preserves_free_minute_cache(tmp_path, monkeypatch, http):
    monkeypatch.setenv("TUSHARE_TOKEN", "test-token")
    monkeypatch.setattr(providers, "_market_now", lambda: datetime(2026, 9, 12, 16))
    bars = [Bar("561560", datetime(2026, 9, 11, 15), 1, 1, 1, 1, 100, source="sina")]
    providers.LocalBarCacheProvider(tmp_path).write_bars("561560", "5m", bars)
    chain = providers.create_market_data_provider(data=DataConfig(tushare_enabled=True), cache_dir=tmp_path)
    assert chain.get_bars("561560", timeframe="5m", limit=1) == bars
    http.assert_not_called()


def test_fundamentals_paid_permission_failure_falls_back_to_free(monkeypatch, http):
    from trade_compass_agent.data.fundamentals import create_fundamentals_provider, FundamentalsSnapshot
    monkeypatch.setenv("TUSHARE_TOKEN", "test-token")
    http.return_value._content = json.dumps({"code": 40203, "msg": "denied"}).encode()
    chain = create_fundamentals_provider(tushare_enabled=True)
    free = next(p for p in chain.providers if p.name == "eastmoney_direct")
    snapshot = FundamentalsSnapshot(symbol="600519", pe_ttm=19.36, provider_name=free.name)
    monkeypatch.setattr(free, "get_snapshot", MagicMock(return_value=snapshot))
    actual = chain.get_snapshot("600519")
    assert actual.pe_ttm == snapshot.pe_ttm and actual.provider_name == snapshot.provider_name
    assert any("tushare" in note for note in actual.notes)
    http.assert_called_once()
    free.get_snapshot.assert_called_once()


@pytest.mark.parametrize("enabled", [False, True])
def test_market_stack_option_controls_web_and_agent_with_existing_cache(client, tmp_path, monkeypatch, http, enabled):
    from trade_compass_agent.config import AppConfig
    from trade_compass_agent.runtime.market_stack import MarketStack
    from trade_compass_agent.runtime.tools.market import tool_get_bars
    from trade_compass_agent.web import api
    monkeypatch.setenv("TUSHARE_TOKEN", "test-token")
    monkeypatch.setattr(providers, "_market_now", lambda: datetime(2026, 9, 12, 16))
    bars = [Bar("561560", datetime(2026, 9, 11, 15), 1, 1, 1, 1, 100, source="tencent")]
    providers.LocalBarCacheProvider(tmp_path / "market_cache").write_bars("561560", "1d", bars)
    config = AppConfig(data_provider="auto", data_dir=tmp_path, data=DataConfig(tushare_enabled=enabled))
    stack = MarketStack.from_config(config)
    monkeypatch.setattr(api, "_stack", lambda: stack)
    result = client.get("/api/bars?symbol=561560&timeframe=1d&limit=1")
    assert result.status_code == 200
    expected = "tushare" if enabled else "tencent"
    assert result.json()["bars"][0]["source"] == expected
    restarted = MarketStack.from_config(config)
    agent = json.loads(tool_get_bars(restarted, symbol="561560", limit=1))
    assert agent["bars"][0]["source"] == expected
    assert http.call_count == int(enabled)
