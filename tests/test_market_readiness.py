from dataclasses import replace
from datetime import datetime
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pandas as pd
import pytest

from trade_compass_agent.config import DataConfig
from trade_compass_agent.data import providers, tushare_provider, fundamentals
from trade_compass_agent.domain import Bar
from trade_compass_agent.runtime.tools import batch, portfolio


def row(code="600519.SH", day="20260911"):
    return dict(ts_code=code, trade_date=day, open=10, high=11, low=9,
                close=10, vol=100, amount=1000)


@pytest.fixture(autouse=True)
def market_clock(monkeypatch):
    monkeypatch.setenv("TUSHARE_TOKEN", "test-token")
    monkeypatch.setattr(providers, "_market_now", lambda: datetime(2026, 9, 12, 16))
    monkeypatch.setattr(tushare_provider, "_market_now", lambda: datetime(2026, 9, 12, 16))


def test_small_paid_batch_uses_one_request_and_preserves_identity(monkeypatch):
    query = MagicMock(return_value=pd.DataFrame([row(), row("000001.SZ")]))
    monkeypatch.setattr(tushare_provider, "query_tushare", query)
    data, errors = tushare_provider.TushareProvider().get_bars_batch(["600519", "000001"], limit=3, timeout=8)
    assert set(data) == {"600519", "000001"} and not errors
    assert query.call_count == 1
    assert query.call_args.kwargs["ts_code"] == "600519.SH,000001.SZ"
    assert data["600519"][0].volume == 10000
    assert data["600519"][0].amount == 1000000
    assert data["600519"][0].source == "tushare"


def test_paid_batch_preserves_fund_lof_and_index_routing(monkeypatch):
    calls = []
    def query(api, **params):
        calls.append((api, params["ts_code"]))
        return pd.DataFrame([row(params["ts_code"])])
    monkeypatch.setattr(tushare_provider, "query_tushare", query)
    symbols = ["600519", "561560", "161725", "sh000001"]
    data, errors = tushare_provider.TushareProvider().get_bars_batch(symbols, limit=3, timeout=8)
    assert set(data) == set(symbols) and not errors
    assert calls == [("daily", "600519.SH"), ("fund_daily", "561560.SH"),
                     ("fund_daily", "161725.SZ"), ("index_daily", "000001.SH")]


def test_native_batch_deadline_bounds_a_hung_request(monkeypatch):
    import time
    def hung_query(*args, **kwargs):
        time.sleep(0.2)
        return pd.DataFrame([row()])
    monkeypatch.setattr(tushare_provider, "query_tushare", hung_query)
    started = time.monotonic()
    data, errors = tushare_provider.TushareProvider().get_bars_batch(["600519"], limit=1, timeout=0.02)
    assert time.monotonic() - started < 0.15
    assert not data and "600519" in errors


@pytest.mark.parametrize("fault", [None, "missing_date", "wrong_date", "truncated", "short_repair_failure"])
def test_universe_uses_daily_cross_sections_without_accepting_partial_history(monkeypatch, fault):
    codes = ["600519", "600201", "000001", "000002"]
    monkeypatch.setattr(tushare_provider, "_MAX_ROWS", 10)
    calls = []
    def query(api, **params):
        calls.append(params)
        if "trade_date" not in params:
            raise providers.ProviderError("short-history repair failed")
        day = params["trade_date"]
        if day == "20260910" and fault == "missing_date":
            return pd.DataFrame()
        frame = pd.DataFrame([row(tushare_provider.to_ts_code(s), day) for s in codes])
        if fault == "wrong_date":
            frame["trade_date"] = "20260909"
        if fault == "truncated":
            frame = pd.concat([frame] * 3)
        if fault == "short_repair_failure" and day == "20260910":
            frame = frame[frame.ts_code != "000002.SZ"]
        return frame
    monkeypatch.setattr(tushare_provider, "query_tushare", query)
    data, errors = tushare_provider.TushareProvider().get_bars_batch(codes, limit=2, timeout=8)
    if fault is None:
        assert set(data) == set(codes) and not errors
        assert [c["trade_date"] for c in calls] == ["20260911", "20260910"]
        assert all(len(bars) == 2 for bars in data.values())
    elif fault == "short_repair_failure":
        assert set(data) == set(codes) - {"000002"}
        assert set(errors) == {"000002"}
    else:
        assert not data and set(errors) == set(codes)


def test_prefetch_failure_falls_back_once_then_retries_paid_on_next_query(monkeypatch, tmp_path):
    chain = providers.create_market_data_provider(data=DataConfig(tushare_enabled=True), cache_dir=tmp_path)
    paid = next(p for p in chain.providers if p.name == "tushare")
    native = MagicMock(return_value=({}, {"600519": "permission denied"}))
    monkeypatch.setattr(paid, "get_bars_batch", native)
    paid_get = MagicMock(side_effect=providers.ProviderError("still unavailable"))
    monkeypatch.setattr(paid, "get_bars", paid_get)
    bars = [Bar("600519", datetime(2026, 9, 11, 15), 10, 11, 9, 10, 100, source="tencent")]
    providers.LocalBarCacheProvider(tmp_path).write_bars("600519", "1d", bars)
    chain.prefetch_bars(["600519"], limit=1)
    assert chain.get_bars("600519", limit=1) == bars
    paid_get.assert_not_called()
    assert chain.get_bars("600519", limit=1) == bars
    paid_get.assert_called_once()


def test_batch_consumer_reports_paid_source_and_reuses_prefetched_cache(monkeypatch, tmp_path):
    chain = providers.create_market_data_provider(data=DataConfig(tushare_enabled=True), cache_dir=tmp_path)
    query = MagicMock(return_value=pd.DataFrame([row(), row("000001.SZ")]))
    monkeypatch.setattr(tushare_provider, "query_tushare", query)
    stack = SimpleNamespace(provider=chain)
    for _ in range(2):
        result = json.loads(batch.tool_batch_get_bars(stack, symbols="600519,000001", limit=1))
        assert result["count"] == 2
        assert {r["source"] for r in result["results"].values()} == {"tushare"}
        assert {r["as_of"] for r in result["results"].values()} == {"2026-09-11T15:00:00"}
    query.assert_called_once()


@pytest.mark.parametrize("fail_paid", [False, True])
def test_batch_fundamentals_obeys_paid_preference_and_falls_back_only_for_missing(monkeypatch, fail_paid):
    frame = pd.DataFrame([dict(ts_code="600519.SH", trade_date="20260911", pe_ttm=20, pb=5, total_mv=100)])
    query = MagicMock(side_effect=providers.ProviderError("denied")) if fail_paid else MagicMock(return_value=frame)
    monkeypatch.setattr(tushare_provider, "query_tushare", query)
    free = MagicMock(return_value=[{"symbol": "600519" if fail_paid else "000001", "pe_ttm": 10}])
    monkeypatch.setattr(batch, "_fetch_ulist_batch", free)
    chain = fundamentals.create_fundamentals_provider(tushare_enabled=True)
    stack = SimpleNamespace(fundamentals_provider=chain)
    symbols = "600519" if fail_paid else "600519,000001"
    result = json.loads(batch.tool_batch_get_fundamentals(stack, symbols=symbols))
    query.assert_called_once()
    if fail_paid:
        assert result["warnings"]
        assert result["results"]["600519"]["provider"] == "eastmoney_ulist_batch"
    else:
        assert result["results"]["600519"]["provider"] == "tushare"
        assert result["results"]["600519"]["total_market_cap"] == 1000000
        free.assert_called_once_with(["0.000001"])


def test_execution_rejects_stale_primary_and_uses_fresh_free_fallback(monkeypatch):
    now = datetime(2026, 9, 14, 10)
    monkeypatch.setattr(providers, "_market_now", lambda: now)
    monkeypatch.setattr(portfolio, "_market_now", lambda: now)
    fresh = Bar("561560", now, 1, 1, 1, 1, 100, source="tencent")
    old = replace(fresh, timestamp=datetime(2026, 9, 14, 9, 50), source="sina")
    primary = SimpleNamespace(name="sina", supported_timeframes={"1m"}, get_bars=lambda *a, **k: [old])
    fallback = SimpleNamespace(name="tencent", supported_timeframes={"1m"}, get_bars=lambda *a, **k: [fresh])
    chain = providers.ChainProvider([primary, fallback])
    assert portfolio._latest_execution_price(SimpleNamespace(provider=chain), "561560") == (1, now)
    assert chain.last_resolved_provider == "tencent"
    fallback.get_bars = primary.get_bars
    with pytest.raises(providers.ProviderError):
        portfolio._latest_execution_price(SimpleNamespace(provider=chain), "561560")


def test_actual_fallback_source_is_recorded_in_cache(tmp_path):
    bar = Bar("600519", datetime(2026, 9, 11, 15), 10, 11, 9, 10, 100)
    fallback = SimpleNamespace(name="baostock", get_bars=lambda *a, **k: [bar])
    cache = providers.LocalBarCacheProvider(tmp_path)
    result = providers.ChainProvider([cache, fallback]).get_bars("600519", limit=1)
    assert result[0].source == "baostock"
    assert cache.get_bars("600519", limit=1)[0].source == "baostock"


def test_missing_etf_fundamentals_are_explicit_not_reported_as_real_data(monkeypatch):
    monkeypatch.setattr(batch, "_fetch_ulist_batch", lambda _: None)
    chain = fundamentals.ChainFundamentalsProvider([fundamentals.TushareFundamentalsProvider(), fundamentals.RuleFundamentalsProvider()])
    stack = SimpleNamespace(fundamentals_provider=chain, provider=SimpleNamespace(get_bars=lambda *a, **k: []))
    result = json.loads(batch.tool_batch_get_fundamentals(stack, symbols="561560"))
    assert result["warnings"]
    assert result["results"]["561560"]["has_real_fundamentals"] is False
    assert result["results"]["561560"]["pe_ttm"] is None
