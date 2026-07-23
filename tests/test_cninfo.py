from __future__ import annotations

from unittest.mock import MagicMock, patch

from trade_compass_agent.data.cninfo import (
    AkshareCninfoProvider,
    SampleCninfoProvider,
    StubCninfoProvider,
    create_cninfo_provider,
)


def test_sample_cninfo_provider_returns_events():
    events = SampleCninfoProvider().get_events("600519", limit=3)
    assert len(events) == 3
    assert all(event.symbol == "600519" for event in events)
    assert all("[样例]" in event.title for event in events)


def test_stub_cninfo_provider_warns():
    events = StubCninfoProvider().get_events("600519")
    assert len(events) == 1
    assert "warning" in events[0].payload


def test_create_cninfo_provider_sample():
    assert create_cninfo_provider("sample").name == "sample"


def test_normalize_a_share_symbol_extracts_six_digits():
    from trade_compass_agent.data.cninfo import _normalize_a_share_symbol

    assert _normalize_a_share_symbol("600519") == "600519"
    assert _normalize_a_share_symbol("sh600519") == "600519"
    assert _normalize_a_share_symbol("600519 短线怎么看") == "600519"


@patch("trade_compass_agent.data.cninfo.run_with_timeout")
def test_akshare_cninfo_provider_parses_dataframe(mock_timeout: MagicMock):
    import pandas as pd

    df = pd.DataFrame(
        {
            "公告标题": ["2024 年度报告", "股东减持计划"],
            "公告日期": ["2024-04-28", "2024-05-01"],
            "公告类型": ["定期报告", "股权变动"],
        }
    )
    mock_timeout.side_effect = lambda func, _timeout, _label: func()
    provider = AkshareCninfoProvider()
    provider.ak = MagicMock()
    provider.ak.stock_individual_notice_report = MagicMock(return_value=df)
    provider.ak.stock_notice_report = MagicMock(side_effect=AssertionError("must not use market-wide notice API"))

    events = provider.get_events("600519", limit=2)
    provider.ak.stock_individual_notice_report.assert_called_once()
    call_kwargs = provider.ak.stock_individual_notice_report.call_args.kwargs
    assert call_kwargs["security"] == "600519"
    assert call_kwargs["symbol"] == "全部"
    assert len(events) == 2
    assert events[0].title == "2024 年度报告"
    assert events[0].source == "akshare/cninfo"


def test_events_api(client):
    response = client.get("/api/events?symbol=600519&limit=3")
    assert response.status_code == 200
    body = response.json()
    assert body["symbol"] == "600519"
    assert len(body["events"]) == 3
