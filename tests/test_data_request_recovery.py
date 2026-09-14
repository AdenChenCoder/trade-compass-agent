"""Regression checks for scheduled analysis failures observed on 2026-09-10."""
from dataclasses import replace
from datetime import date, datetime
import json
import os
import socket
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from trade_compass_agent.data import providers
from trade_compass_agent.data.providers import LocalBarCacheProvider, ProviderError, SinaDailyProvider
from trade_compass_agent.domain import Bar, LimitUpSummary


def bar(timestamp):
    return Bar(symbol="161725", timestamp=datetime.fromisoformat(timestamp), open=1, high=2, low=1, close=2, volume=100)


@pytest.mark.parametrize("symbol,expected", [
    ("161725", "sz161725"), ("sh000001", "sh000001"), ("000001", "sz000001"),
    ("sh.000001", "sh000001"), ("000001.SH", "sh000001"), ("510300", "sh510300"),
])
def test_daily_request_uses_correct_exchange_and_preserves_identity(monkeypatch, symbol, expected):
    response = MagicMock()
    response.json.return_value = [{"day": "2026-09-10", "open": "1", "high": "2", "low": "1", "close": "2"}]
    get = MagicMock(return_value=response)
    monkeypatch.setattr("requests.get", get)
    bars = SinaDailyProvider().get_bars(symbol, limit=1)
    assert get.call_args.kwargs["params"]["symbol"] == expected
    assert bars[0].symbol == symbol
    assert bars[0].timestamp.date() == date(2026, 9, 10)


@pytest.mark.parametrize("symbol,method", [("161725", "fund_lof_hist_em"), ("sh000001", "index_zh_a_hist"), ("000001", "stock_zh_a_hist")])
def test_akshare_selects_fund_index_or_stock_endpoint(monkeypatch, symbol, method):
    import pandas as pd
    provider = providers.AkshareProvider.__new__(providers.AkshareProvider)
    provider.ak = MagicMock()
    provider.timeout = 1
    getattr(provider.ak, method).return_value = pd.DataFrame([{"日期": "2026-09-10", "开盘": 1, "最高": 2, "最低": 1, "收盘": 2}])
    assert len(provider.get_bars(symbol, limit=1)) == 1
    assert provider.ak.method_calls[0][0] == method
    assert getattr(provider.ak, method).call_args.kwargs["symbol"] == providers.split_symbol(symbol)[1]


def test_after_close_rejects_yesterday_and_retries_next_source(monkeypatch):
    monkeypatch.setattr(providers, "_market_now", lambda: datetime(2026, 9, 10, 15, 10))
    yesterday = SimpleNamespace(name="old", get_bars=lambda *a, **k: [bar("2026-09-09")])
    today = SimpleNamespace(name="current", get_bars=lambda *a, **k: [bar("2026-09-10")])
    chain = providers.ChainProvider([yesterday, today])
    assert chain.get_bars("161725", limit=1)[-1].timestamp.date() == date(2026, 9, 10)
    assert chain.last_resolved_provider == "current"
    with pytest.raises(ProviderError, match="stale daily bars"):
        providers.ChainProvider([yesterday]).get_bars("161725", limit=1)


def test_empty_provider_response_does_not_count_as_success():
    source = SimpleNamespace(name="empty", get_bars=lambda *a, **k: [])
    with pytest.raises(ProviderError, match="empty bars response"):
        providers.ChainProvider([source]).get_bars("161725")


def test_cache_refresh_keeps_history_and_updates_latest(tmp_path, monkeypatch):
    monkeypatch.setattr(providers, "_market_now", lambda: datetime(2026, 9, 10, 15, 10))
    cache = LocalBarCacheProvider(tmp_path)
    cache.write_bars("161725", "1d", [bar("2026-09-08"), bar("2026-09-09")])
    with pytest.raises(ProviderError, match="stale cache"):
        cache.get_bars("161725", limit=1)
    cache.write_bars("161725", "1d", [bar("2026-09-10")])
    assert len(cache.get_bars("161725", limit=3)) == 3
    assert cache.get_bars("161725", limit=1)[0].timestamp.date() == date(2026, 9, 10)


def test_daily_cache_rejects_intraday_snapshot_after_close(tmp_path, monkeypatch):
    monkeypatch.setattr(providers, "_market_now", lambda: datetime(2026, 9, 10, 15, 10))
    cache = LocalBarCacheProvider(tmp_path)
    cache.write_bars("161725", "1d", [bar("2026-09-10")])
    from zoneinfo import ZoneInfo
    stamp = datetime(2026, 9, 10, 14, 30, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()
    os.utime(cache._path("161725", "1d"), (stamp, stamp))
    with pytest.raises(ProviderError, match="stale cache"):
        cache.get_bars("161725", limit=1)


@pytest.mark.parametrize("now,latest,accepted", [
    ("2026-09-10T10:05:00", "2026-09-10T09:40:00", False),
    ("2026-09-10T10:05:00", "2026-09-10T10:00:00", True),
    ("2026-09-10T12:00:00", "2026-09-10T11:30:00", True),
    ("2026-09-10T15:10:00", "2026-09-09T15:00:00", False),
])
def test_minute_cache_checks_timestamp(tmp_path, monkeypatch, now, latest, accepted):
    monkeypatch.setattr(providers, "_market_now", lambda: datetime.fromisoformat(now))
    cache = LocalBarCacheProvider(tmp_path)
    cache.write_bars("161725", "5m", [bar(latest)])
    if accepted:
        assert cache.get_bars("161725", "5m", 1)
    else:
        with pytest.raises(ProviderError, match="stale cache"):
            cache.get_bars("161725", "5m", 1)


def test_long_lived_bulk_provider_recomputes_date(monkeypatch):
    provider = providers.BulkDailyBarProvider.__new__(providers.BulkDailyBarProvider)
    for now, expected in [(datetime(2026, 9, 10, 14), date(2026, 9, 9)), (datetime(2026, 9, 10, 15, 10), date(2026, 9, 10)), (datetime(2026, 9, 12, 16), date(2026, 9, 11))]:
        monkeypatch.setattr(providers, "_market_now", lambda: now)
        assert provider._get_min_date() == expected


@pytest.mark.parametrize("primary", [[], TimeoutError("upstream timeout")])
def test_market_pulse_uses_real_fallback_and_preserves_unknown_counts(monkeypatch, primary):
    from trade_compass_agent.data import market_pulse
    p = market_pulse.AkshareMarketPulseProvider.__new__(market_pulse.AkshareMarketPulseProvider)
    p._fetch_sector_strength = MagicMock(side_effect=primary) if isinstance(primary, Exception) else lambda: primary
    p._fetch_limit_up_summary = lambda: LimitUpSummary(count=35, strong_count=4, top_industries=[], leaders=[])
    monkeypatch.setattr(market_pulse, "fetch_sina_board_rows", lambda **k: [{"f14": "行业", "f3": 2.3}])
    pulse = p.get_market_pulse()
    assert pulse.sectors[0].change_pct == 2.3
    assert pulse.sectors[0].up_count is None
    assert pulse.limit_up.count == 35
    assert "新浪" in pulse.warnings[0]


def test_web_search_switches_engines_after_failure_and_empty(monkeypatch):
    from trade_compass_agent.runtime.tools.search import tool_web_search
    instance = MagicMock()
    instance.__enter__.return_value = instance
    instance.text.side_effect = [TimeoutError("timeout"), [], [{"title": "result", "href": "https://example.com", "body": "summary"}]]
    monkeypatch.setitem(sys.modules, "ddgs", SimpleNamespace(DDGS=MagicMock(return_value=instance)))
    monkeypatch.setitem(sys.modules, "ddgs.engines", SimpleNamespace(ENGINES={"text": dict.fromkeys(["brave", "duckduckgo", "yahoo", "google"])}))
    monkeypatch.setenv("WEB_SEARCH_PROVIDER", "auto")
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    result = json.loads(tool_web_search(query="market", limit=1))
    assert result["provider"] == "yahoo"
    assert result["count"] == 1
    assert len(result["warnings"]) == 2
    assert [call.kwargs["backend"] for call in instance.text.call_args_list] == ["brave", "duckduckgo", "yahoo"]


def test_web_search_all_fail_is_error(monkeypatch):
    from trade_compass_agent.runtime.tools.search import tool_web_search
    instance = MagicMock()
    instance.__enter__.return_value = instance
    instance.text.return_value = []
    monkeypatch.setitem(sys.modules, "ddgs", SimpleNamespace(DDGS=MagicMock(return_value=instance)))
    monkeypatch.setitem(sys.modules, "ddgs.engines", SimpleNamespace(ENGINES={"text": dict.fromkeys(["brave", "duckduckgo", "yahoo", "google"])}))
    monkeypatch.setenv("WEB_SEARCH_PROVIDER", "auto")
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    result = json.loads(tool_web_search(query="market"))
    assert result["error"]
    assert result["results"] == []
    assert instance.text.call_count == 4


def test_skill_quality_uses_real_tool_catalog(tmp_path):
    from trade_compass_agent.memory.skill_store import SkillStore
    store = SkillStore(tmp_path / "skills")
    content = "---\nname: news-check\ndescription: Review news and announcements\n---\n## Steps\n1. search_stock_news()\n2. search_announcements()\n"
    result = store.create("news-check", content, created_by="agent")
    assert result["ok"] is True
    assert store.get("news-check") is not None


def test_unconfigured_delivery_is_visible_without_attempt(client, monkeypatch, tmp_path):
    from trade_compass_agent.channels.base import ChannelRouter
    from trade_compass_agent.channels.weixin import WeixinBotAdapter
    from trade_compass_agent.config import load_app_config
    from trade_compass_agent.ops.delivery import DeliveryRouter
    from trade_compass_agent.ops.job_definition import DeliveryConfig
    from trade_compass_agent.ops.run_store import SqliteRunStore
    config = load_app_config()
    config = replace(config, notifications=replace(config.notifications, macos_enabled=False))
    store = SqliteRunStore(config.data_dir / "scheduler.db")
    run = store.create_run("premarket")
    store.complete_run(run, message="分析仍可查看")
    router = ChannelRouter()
    adapter = WeixinBotAdapter(credentials_path=tmp_path / "credentials.json")
    adapter.send_sync = MagicMock()
    router.register(adapter)
    monkeypatch.setattr("trade_compass_agent.ops.delivery._build_channel_router", lambda: router)
    DeliveryRouter(config).deliver(run, DeliveryConfig(channels=("weixin", "wecom")))
    result = client.get(f"/api/jobs/runs/{run.id}").json()
    assert result["status"] == "completed"
    assert result["message"] == "分析仍可查看"
    steps = {s["step_id"]: s for s in result["step_runs"]}
    assert steps["微信消息发送"]["status"] == "skipped"
    assert "接收人" in steps["微信消息发送"]["output"]
    assert "未连接" in steps["企业微信消息发送"]["output"]
    adapter.send_sync.assert_not_called()


def test_llm_dns_failure_exposes_actionable_cause():
    from trade_compass_agent.llm.providers import _request_error_detail
    cause = socket.gaierror(-2, "Name or service not known")
    error = RuntimeError("Connection error.")
    error.__cause__ = cause
    assert "DNS" in _request_error_detail(error)


def test_daily_cache_deduplicates_dates_and_never_mixes_adjustments(tmp_path, monkeypatch):
    monkeypatch.setattr(providers, "_market_now", lambda: datetime(2026, 9, 10, 16))
    cache = LocalBarCacheProvider(tmp_path)
    cache.write_bars("161725", "1d", [bar("2026-09-09"), bar("2026-09-10")])
    cache.write_bars("161725", "1d", [replace(bar("2026-09-10T15:00:00"), close=3)])
    rows = cache.get_bars("161725", limit=2)
    assert rows[-1].close == 3
    cache.write_bars("161725", "1d", [replace(bar("2026-09-10"), adjusted=True)])
    assert cache.get_bars("161725", limit=1)[0].adjusted is True
    with pytest.raises(ProviderError, match="need 2"):
        cache.get_bars("161725", limit=2)


def test_cancelled_model_request_is_not_sent(monkeypatch):
    from trade_compass_agent.llm.providers import OpenAIChatClient
    from trade_compass_agent.runtime.exceptions import TurnInterruptedError
    client = OpenAIChatClient.__new__(OpenAIChatClient)
    client.name, client.model, client.max_retries = "openai", "test", 2
    client._client = MagicMock()
    with pytest.raises(TurnInterruptedError):
        client.stream_complete([], is_cancelled=lambda: True)
    client._client.chat.completions.create.assert_not_called()


def test_failed_stream_closed_and_cancel_during_backoff_prevents_replay(monkeypatch):
    import httpx
    from trade_compass_agent.llm.providers import OpenAIChatClient
    from trade_compass_agent.runtime.exceptions import TurnInterruptedError
    client = OpenAIChatClient.__new__(OpenAIChatClient)
    client.name, client.model, client.max_retries = "openai", "test", 2
    client._client = MagicMock()
    stream = MagicMock()
    stream.__iter__.side_effect = httpx.ReadTimeout("timeout")
    client._client.chat.completions.create.return_value = stream
    cancelled = [False]
    monkeypatch.setattr("trade_compass_agent.llm.providers.time.sleep", lambda _: cancelled.__setitem__(0, True))
    with pytest.raises(TurnInterruptedError):
        client.stream_complete([], is_cancelled=lambda: cancelled[0])
    stream.close.assert_called_once()
    client._client.chat.completions.create.assert_called_once()


def test_web_search_honours_explicit_proxy(monkeypatch):
    from trade_compass_agent.runtime.tools.search import _web_search_proxy
    monkeypatch.setenv("DDGS_PROXY", "http://proxy.example:8080")
    assert _web_search_proxy() == "http://proxy.example:8080"
