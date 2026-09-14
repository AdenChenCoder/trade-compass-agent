"""Consumer regressions for sources that returned misleading fallback data."""
import json
import socket
import threading
from unittest.mock import MagicMock

import pytest
import requests

from trade_compass_agent.runtime.tools import search


def response(payload):
    result = MagicMock()
    result.json.return_value = payload
    return result


def test_cls_current_endpoint_returns_newest_news_with_full_shanghai_date(monkeypatch):
    get = MagicMock(return_value=response({"errno": 0, "data": {"roll_data": [
        {"ctime": 1789056000, "content": "earlier"},
        {"ctime": 1789058126, "content": "latest"},
        {"ctime": None, "content": "missing date"},
    ]}}))
    monkeypatch.setattr(requests, "get", get)
    result = json.loads(search.tool_search_market_flash(limit=1))
    assert result == {"count": 1, "alerts": [{"time": "2026-09-11 00:35:26", "content": "latest"}], "source": "cls"}
    assert get.call_args.args[0] == "https://www.cls.cn/api/cache"
    assert get.call_args.kwargs["params"]["lastTime"] > 0
    assert get.call_args.kwargs["timeout"] == (2, 3)
    assert get.call_count == 1


@pytest.mark.parametrize("primary", ["404", "empty"])
def test_flash_fallback_is_flash_news_with_failure_provenance(monkeypatch, primary):
    cls = response({"errno": 0, "data": {"roll_data": []}})
    if primary == "404":
        cls.raise_for_status.side_effect = requests.HTTPError("404 Not Found")
    fallback = response({"code": "1", "data": {"fastNewsList": [
        {"showTime": "2026-09-11 00:30:00", "summary": "原油价格变化"},
        {"showTime": "2026-09-11 00:35:00", "summary": "最新财经快讯"},
    ]}})
    get = MagicMock(side_effect=[cls, fallback])
    monkeypatch.setattr(requests, "get", get)
    monkeypatch.setattr(search, "rate_limit_domain", lambda url: None)
    result = json.loads(search.tool_search_market_flash(limit=1))
    assert result["source"] == "eastmoney_flash"
    assert result["alerts"][0]["content"] == "最新财经快讯"
    assert "cls:" in result["warnings"][0]
    assert "getFastNewsList" in get.call_args.args[0]
    assert "req_trace" in get.call_args.kwargs["params"]
    assert get.call_count == 2


def test_flash_upstream_application_error_is_failure_even_with_http_200(monkeypatch):
    monkeypatch.setattr(requests, "get", lambda *a, **k: response({"code": 0, "message": "missing parameter", "data": None}))
    result = json.loads(search.tool_search_market_flash())
    assert result["alerts"] == []
    assert result["count"] == 0
    assert "error" in result


def test_sina_board_unknown_turnover_is_never_presented_as_percentage(monkeypatch):
    from trade_compass_agent.data.sector_boards import fetch_sina_board_rows
    monkeypatch.setattr(requests, "get", lambda *a, **k: response([
        {"name": "船舶制造", "avg_changeratio": "0.0226825", "turnover": "229.533", "ts_name": "中国船舶"},
    ]))
    rows = fetch_sina_board_rows(board_type="industry", limit=1)
    result = search._parse_em_board_row(rows[0], include_counts=True)
    assert result["change_pct"] == pytest.approx(2.26825)
    assert result["turnover_pct"] is None
    assert result["up_count"] is None


def test_market_pulse_primary_uses_shared_bounded_reader(monkeypatch):
    from trade_compass_agent.data import market_pulse
    provider = market_pulse.AkshareMarketPulseProvider.__new__(market_pulse.AkshareMarketPulseProvider)
    provider.timeout = 2
    get = MagicMock(return_value=response({"data": {"diff": [{
        "f14": "行业", "f3": 2, "f8": 1.5, "f104": 8, "f105": 2,
        "f128": "领涨股", "f136": 5,
    }]}}))
    monkeypatch.setattr(requests, "get", get)
    sectors = provider._fetch_sector_strength()
    assert sectors[0].up_count == 8
    assert sectors[0].turnover_pct == 1.5
    assert sectors[0].leader_change_pct == 5
    assert get.call_count == 1
    assert get.call_args.kwargs["params"]["pz"] == "8"
    assert get.call_args.kwargs["timeout"] == (1.0, 1.5)


def test_overlapping_deadlines_never_change_other_requests_socket_timeout():
    from trade_compass_agent.data.network import run_with_timeout
    original = socket.getdefaulttimeout()
    started = [threading.Event(), threading.Event()]
    release = [threading.Event(), threading.Event()]
    observed = []

    def fetch(index):
        started[index].set()
        assert release[index].wait(2)
        observed.append(socket.getdefaulttimeout())

    callers = [threading.Thread(target=lambda i=i: run_with_timeout(lambda: fetch(i), 4 + i, "overlap")) for i in range(2)]
    try:
        for i, caller in enumerate(callers):
            caller.start()
            assert started[i].wait(1)
        release[0].set()
        callers[0].join(1)
        release[1].set()
        callers[1].join(1)
        assert all(not caller.is_alive() for caller in callers)
        assert observed == [original, original]
        assert socket.getdefaulttimeout() == original
    finally:
        for event in release:
            event.set()
        for caller in callers:
            if caller.ident:
                caller.join(2)
        socket.setdefaulttimeout(original)
