"""Tests for search tools (stock news, announcements, web search, flash, hot, lhb, concepts, xueqiu, activity, industry, research, recommend, x)."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from trade_compass_agent.runtime.tools.search import (
    tool_search_announcements,
    tool_search_concept_boards,
    tool_search_hot_stocks,
    tool_search_industry_boards,
    tool_search_institute_recommend,
    tool_search_lhb,
    tool_search_market_activity,
    tool_search_market_flash,
    tool_search_research_reports,
    tool_search_stock_news,
    tool_search_x,
    tool_search_xueqiu_hot,
    tool_web_search,
)
from trade_compass_agent.runtime.tools.batch import tool_batch_search_news


@pytest.fixture
def stack():
    from trade_compass_agent.runtime.market_stack import MarketStack

    return MarketStack.from_config()


class TestSearchStockNews:
    def test_returns_news_on_success(self, stack):
        fake_jsonp = json.dumps({
            "result": {
                "cmsArticleWebOld": [
                    {
                        "title": "贵州茅台业绩超预期",
                        "content": "茅台2024年净利润同比增长15%...",
                        "date": "2024-03-15 10:30:00",
                        "mediaName": "东方财富",
                        "url": "https://example.com/news/1",
                    },
                    {
                        "title": "白酒板块集体走强",
                        "content": "白酒板块今日领涨...",
                        "date": "2024-03-15 09:45:00",
                        "mediaName": "新浪财经",
                        "url": "https://example.com/news/2",
                    },
                ]
            }
        })
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.text = fake_jsonp

        with patch("requests.get", return_value=mock_resp):
            result = json.loads(tool_search_stock_news(stack, symbol="600519", limit=5))

        assert result["symbol"] == "600519"
        assert result["count"] == 2
        assert result["news"][0]["title"] == "贵州茅台业绩超预期"
        assert result["news"][0]["source"] == "东方财富"
        assert "url" in result["news"][0]

    def test_returns_empty_on_no_data(self, stack):
        fake_jsonp = json.dumps({"result": {"cmsArticleWebOld": []}})
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.text = fake_jsonp

        with patch("requests.get", return_value=mock_resp):
            result = json.loads(tool_search_stock_news(stack, symbol="000001", limit=3))

        assert result["count"] == 0
        assert result["news"] == []

    def test_batch_search_news_bounds_each_request(self, stack):
        with patch(
            "trade_compass_agent.runtime.tools.batch.run_with_timeout",
            side_effect=TimeoutError("news request timed out"),
        ) as timeout_guard:
            result = json.loads(
                tool_batch_search_news(stack, symbols="600519,000001", limit_per_symbol=2)
            )

        assert result["count"] == 0
        assert set(result["errors"]) == {"600519", "000001"}
        assert timeout_guard.call_count == 2

    def test_returns_error_on_timeout(self, stack):
        with patch("requests.get", side_effect=TimeoutError("timed out")):
            result = json.loads(tool_search_stock_news(stack, symbol="600519"))

        assert "error" in result
        assert result["news"] == []


class TestSearchAnnouncements:
    def test_returns_announcements(self, stack):
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "data": {
                "list": [
                    {"title": "关于回购股份的公告", "notice_date": "2024-03-10 08:00:00"},
                ]
            }
        }

        with patch("requests.get", return_value=mock_resp):
            result = json.loads(tool_search_announcements(stack, symbol="600519", limit=5))

        assert result["count"] == 1
        assert result["announcements"][0]["title"] == "关于回购股份的公告"

    def test_returns_empty_on_api_error(self, stack):
        with patch("requests.get", side_effect=Exception("API error")):
            result = json.loads(tool_search_announcements(stack, symbol="600519", limit=5))

        assert result["announcements"] == []
        assert "error" in result


class TestWebSearch:
    def test_uses_ddg_without_api_key(self):
        import sys

        mock_module = MagicMock()
        mock_ddgs_instance = MagicMock()
        mock_ddgs_instance.__enter__ = MagicMock(return_value=mock_ddgs_instance)
        mock_ddgs_instance.__exit__ = MagicMock(return_value=False)
        mock_ddgs_instance.text.return_value = [
            {"title": "A股分析", "href": "https://example.com", "body": "市场近期表现..."},
        ]
        mock_module.DDGS.return_value = mock_ddgs_instance
        sys.modules["duckduckgo_search"] = mock_module

        try:
            with patch.dict("os.environ", {"TAVILY_API_KEY": ""}, clear=False):
                result = json.loads(tool_web_search(query="A股市场趋势", limit=3))
        finally:
            del sys.modules["duckduckgo_search"]

        assert result["provider"] == "duckduckgo"
        assert result["count"] == 1
        assert result["results"][0]["title"] == "A股分析"

    def test_uses_tavily_with_api_key(self):
        import sys

        mock_module = MagicMock()
        mock_client_instance = MagicMock()
        mock_client_instance.search.return_value = {
            "answer": "A股市场近期震荡上行",
            "results": [
                {"title": "A股市场分析", "url": "https://example.com/analysis", "content": "近期市场表现良好..."},
            ],
        }
        mock_module.TavilyClient.return_value = mock_client_instance
        sys.modules["tavily"] = mock_module

        try:
            with patch.dict("os.environ", {"TAVILY_API_KEY": "test-key"}, clear=False):
                result = json.loads(tool_web_search(query="A股趋势", limit=3))
        finally:
            del sys.modules["tavily"]

        assert result["provider"] == "tavily"
        assert result["query"] == "A股趋势"
        assert result["answer"] == "A股市场近期震荡上行"
        assert result["count"] == 1

    def test_tavily_import_fails_falls_to_ddg(self):
        import sys

        sys.modules["tavily"] = None  # type: ignore

        mock_ddg = MagicMock()
        mock_ddgs_instance = MagicMock()
        mock_ddgs_instance.__enter__ = MagicMock(return_value=mock_ddgs_instance)
        mock_ddgs_instance.__exit__ = MagicMock(return_value=False)
        mock_ddgs_instance.text.return_value = [
            {"title": "Result", "href": "https://x.com", "body": "content"},
        ]
        mock_ddg.DDGS.return_value = mock_ddgs_instance
        sys.modules["duckduckgo_search"] = mock_ddg

        try:
            with patch.dict("os.environ", {"TAVILY_API_KEY": "key"}, clear=False):
                result = json.loads(tool_web_search(query="test"))
        finally:
            del sys.modules["tavily"]
            del sys.modules["duckduckgo_search"]

        assert result["provider"] == "duckduckgo"


class TestMarketFlash:
    def test_returns_alerts(self):
        import pandas as pd

        fake_df = pd.DataFrame([
            {"时间": "2024-03-15 10:30:00", "快讯信息": "【快讯：白酒板块集体走强】贵州茅台涨3%"},
            {"时间": "2024-03-15 10:25:00", "快讯信息": "【快讯：半导体板块异动】中芯国际拉升"},
        ])

        with patch("trade_compass_agent.runtime.tools.search.run_with_timeout", return_value=fake_df):
            result = json.loads(tool_search_market_flash(limit=5))

        assert result["count"] == 2
        assert "白酒" in result["alerts"][0]["content"]

    def test_returns_empty_on_error(self):
        with (
            patch("trade_compass_agent.runtime.tools.search.run_with_timeout", side_effect=TimeoutError("timeout")),
            patch("requests.get", side_effect=Exception("fallback failed")),
        ):
            result = json.loads(tool_search_market_flash())

        assert result["alerts"] == []
        assert "error" in result


class TestHotStocks:
    def test_returns_ranking(self):
        fake_rows = [
            {"symbol": "600519", "name": "贵州茅台", "rank": 1, "change_pct": 2.5},
            {"symbol": "300750", "name": "宁德时代", "rank": 2, "change_pct": -1.2},
        ]

        with patch(
            "trade_compass_agent.runtime.tools.search._fetch_hot_stock_rows",
            return_value=(fake_rows, "eastmoney"),
        ):
            result = json.loads(tool_search_hot_stocks(limit=10))

        assert result["count"] == 2
        assert result["stocks"][0]["symbol"] == "600519"
        assert result["stocks"][0]["rank"] == 1

    def test_returns_empty_on_error(self):
        with patch(
            "trade_compass_agent.runtime.tools.search.run_with_timeout",
            side_effect=Exception("network error"),
        ):
            result = json.loads(tool_search_hot_stocks())

        assert result["stocks"] == []

    def test_falls_back_to_sina(self):
        fake_rows = [{"symbol": "002851", "name": "麦格米特", "rank": 1, "change_pct": 10.0}]
        with patch(
            "trade_compass_agent.runtime.tools.search._fetch_em_hot_stock_rows",
            side_effect=Exception("push2 down"),
        ), patch(
            "trade_compass_agent.runtime.tools.search._fetch_sina_hot_stock_rows",
            return_value=fake_rows,
        ):
            result = json.loads(tool_search_hot_stocks(limit=5))

        assert result["count"] == 1
        assert result["source"] == "sina"


class TestLhb:
    def test_returns_entries(self):
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "success": True,
            "result": {
                "data": [
                    {
                        "SECURITY_CODE": "600519",
                        "SECURITY_NAME_ABBR": "贵州茅台",
                        "TRADE_DATE": "2024-03-15",
                        "EXPLANATION": "日涨幅偏离值达7%",
                        "BILLBOARD_NET_AMT": 50000000,
                        "CHANGE_RATE": 7.0,
                    },
                ]
            },
        }

        with patch("requests.get", return_value=mock_resp):
            result = json.loads(tool_search_lhb(limit=5))

        assert result["count"] == 1
        assert result["entries"][0]["symbol"] == "600519"
        assert result["entries"][0]["net_buy"] == 0.5

    def test_filters_by_symbol(self):
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "success": True,
            "result": {
                "data": [
                    {
                        "SECURITY_CODE": "600519",
                        "SECURITY_NAME_ABBR": "贵州茅台",
                        "TRADE_DATE": "2024-03-15",
                        "EXPLANATION": "涨幅偏离",
                        "BILLBOARD_NET_AMT": 5000,
                        "CHANGE_RATE": 3.0,
                    },
                ]
            },
        }

        with patch("requests.get", return_value=mock_resp):
            result = json.loads(tool_search_lhb(symbol="600519", limit=5))

        assert result["count"] == 1
        assert result["entries"][0]["symbol"] == "600519"

    def test_returns_empty_on_timeout(self):
        with patch("requests.get", side_effect=TimeoutError("timeout")):
            result = json.loads(tool_search_lhb())

        assert result["entries"] == []
        assert "error" in result


class TestConceptBoards:
    def test_returns_boards(self):
        fake_rows = [
            {"f14": "人工智能", "f3": 3.5, "f128": "科大讯飞", "f8": 5.2},
            {"f14": "新能源车", "f3": 2.1, "f128": "比亚迪", "f8": 3.8},
        ]

        with patch(
            "trade_compass_agent.runtime.tools.search._fetch_board_rows",
            return_value=(fake_rows, "eastmoney"),
        ):
            result = json.loads(tool_search_concept_boards(limit=10))

        assert result["count"] == 2
        assert result["boards"][0]["name"] == "人工智能"
        assert result["boards"][0]["change_pct"] == 3.5

    def test_returns_empty_on_error(self):
        with patch(
            "trade_compass_agent.runtime.tools.search.run_with_timeout",
            side_effect=Exception("error"),
        ):
            result = json.loads(tool_search_concept_boards())

        assert result["boards"] == []


    def test_falls_back_to_sina(self):
        fake_rows = [
            {"f14": "石墨烯", "f3": 5.3, "f128": "ST南都", "f8": 403.0},
        ]
        with patch(
            "trade_compass_agent.runtime.tools.search._fetch_em_board_rows",
            side_effect=Exception("push2 down"),
        ), patch(
            "trade_compass_agent.runtime.tools.search._fetch_sina_board_rows",
            return_value=fake_rows,
        ):
            result = json.loads(tool_search_concept_boards(limit=5))

        assert result["count"] == 1
        assert result["source"] == "sina"
        assert result["boards"][0]["name"] == "石墨烯"


class TestXueqiuHot:
    def test_returns_stocks(self):
        fake_sina = [
            {"symbol": "sh600519", "name": "贵州茅台", "netamount": "5000000000", "changeratio": "0.025"},
            {"symbol": "sz300750", "name": "宁德时代", "netamount": "3000000000", "changeratio": "-0.012"},
        ]

        with patch("requests.get") as mock_get:
            mock_resp = mock_get.return_value
            mock_resp.raise_for_status = lambda: None
            mock_resp.json.return_value = fake_sina
            result = json.loads(tool_search_xueqiu_hot(limit=10))

        assert result["count"] == 2
        assert result["stocks"][0]["name"] == "贵州茅台"
        assert result["stocks"][0]["symbol"] == "600519"
        assert result["source"] == "sina_moneyflow"

    def test_returns_empty_on_error(self):
        with patch("requests.get", side_effect=Exception("timeout")):
            result = json.loads(tool_search_xueqiu_hot())

        assert result["stocks"] == []


class TestMarketActivity:
    def test_returns_activities(self):
        import pandas as pd

        fake_df = pd.DataFrame([
            {"代码": "600519", "名称": "贵州茅台", "时间": "10:15:00"},
            {"代码": "300750", "名称": "宁德时代", "时间": "10:20:00"},
        ])

        with patch("trade_compass_agent.runtime.tools.search.run_with_timeout", return_value=fake_df):
            result = json.loads(tool_search_market_activity(limit=10))

        assert result["count"] > 0
        assert result["activities"][0]["symbol"] == "600519"
        assert result["activities"][0]["event_type"] == "火箭发射"

    def test_returns_empty_on_error(self):
        with patch(
            "trade_compass_agent.runtime.tools.search.run_with_timeout",
            side_effect=Exception("fail"),
        ):
            result = json.loads(tool_search_market_activity())

        assert result["activities"] == []
        assert result["count"] == 0


class TestIndustryBoards:
    def test_returns_boards(self):
        fake_rows = [
            {"f14": "白酒", "f3": 4.2, "f128": "贵州茅台", "f8": 2.5, "f104": 8, "f105": 1},
            {"f14": "半导体", "f3": -1.3, "f128": "中芯国际", "f8": 3.1, "f104": 3, "f105": 15},
        ]

        with patch(
            "trade_compass_agent.runtime.tools.search._fetch_board_rows",
            return_value=(fake_rows, "eastmoney"),
        ):
            result = json.loads(tool_search_industry_boards(limit=10))

        assert result["count"] == 2
        assert result["boards"][0]["name"] == "白酒"
        assert result["boards"][0]["up_count"] == 8

    def test_returns_empty_on_error(self):
        with patch(
            "trade_compass_agent.runtime.tools.search.run_with_timeout",
            side_effect=Exception("error"),
        ):
            result = json.loads(tool_search_industry_boards())

        assert result["boards"] == []


class TestResearchReports:
    def test_returns_reports(self):
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "data": [
                {
                    "title": "茅台2024年报点评：业绩稳健增长",
                    "orgSName": "中信证券",
                    "researcher": "张三",
                    "publishDate": "2024-03-18",
                    "emRatingName": "买入",
                },
            ]
        }

        with patch("requests.get", return_value=mock_resp):
            result = json.loads(tool_search_research_reports(symbol="600519", limit=5))

        assert result["symbol"] == "600519"
        assert result["count"] == 1
        assert "茅台" in result["reports"][0]["title"]
        assert result["reports"][0]["org"] == "中信证券"

    def test_returns_empty_on_error(self):
        with patch("requests.get", side_effect=TimeoutError("timeout")):
            result = json.loads(tool_search_research_reports(symbol="600519"))

        assert result["reports"] == []


class TestInstituteRecommend:
    def test_returns_recommendations(self):
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "data": [
                {"orgSName": "中信证券", "emRatingName": "买入", "predictThisYearEps": "2100", "publishDate": "2024-03-15", "title": "茅台点评"},
                {"orgSName": "国泰君安", "emRatingName": "增持", "predictThisYearEps": "1980", "publishDate": "2024-03-10", "title": "茅台调研"},
            ]
        }

        with patch("requests.get", return_value=mock_resp):
            result = json.loads(tool_search_institute_recommend(symbol="600519", limit=5))

        assert result["symbol"] == "600519"
        assert result["count"] == 2
        assert result["recommendations"][0]["org"] == "中信证券"
        assert result["recommendations"][0]["rating"] == "买入"

    def test_returns_empty_on_error(self):
        with patch("requests.get", side_effect=Exception("no data")):
            result = json.loads(tool_search_institute_recommend(symbol="000001"))

        assert result["recommendations"] == []


class TestSearchX:
    def test_returns_error_without_api_key(self):
        with patch.dict("os.environ", {"XAI_API_KEY": ""}, clear=False):
            result = json.loads(tool_search_x(query="NVDA earnings"))

        assert "error" in result
        assert "XAI_API_KEY" in result["error"]

    def test_returns_results_with_api_key(self):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "output": [
                {
                    "type": "message",
                    "content": [
                        {
                            "type": "output_text",
                            "text": "NVDA reported strong Q1 earnings...",
                            "annotations": [
                                {
                                    "type": "url_citation",
                                    "title": "@Serenity on NVDA",
                                    "url": "https://x.com/Serenity/status/123",
                                },
                            ],
                        }
                    ],
                }
            ]
        }

        with patch.dict("os.environ", {"XAI_API_KEY": "xai-test-key"}, clear=False):
            with patch("httpx.Client") as mock_client_cls:
                mock_client = MagicMock()
                mock_client.__enter__ = MagicMock(return_value=mock_client)
                mock_client.__exit__ = MagicMock(return_value=False)
                mock_client.post.return_value = mock_response
                mock_client_cls.return_value = mock_client

                result = json.loads(tool_search_x(
                    query="NVDA earnings",
                    handles=["Serenity"],
                    days_back=3,
                ))

        assert result["provider"] == "xai_x_search"
        assert "NVDA" in result["answer"]
        assert result["citations"][0]["title"] == "@Serenity on NVDA"
        assert result["handles_filter"] == ["Serenity"]

    def test_handles_api_error(self):
        with patch.dict("os.environ", {"XAI_API_KEY": "xai-key"}, clear=False):
            with patch("httpx.Client") as mock_client_cls:
                mock_client = MagicMock()
                mock_client.__enter__ = MagicMock(return_value=mock_client)
                mock_client.__exit__ = MagicMock(return_value=False)
                mock_client.post.side_effect = Exception("connection refused")
                mock_client_cls.return_value = mock_client

                result = json.loads(tool_search_x(query="test"))

        assert "error" in result
        assert "xAI" in result["error"]
