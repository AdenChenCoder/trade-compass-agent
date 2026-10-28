from __future__ import annotations

import json
import threading
import time
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import requests

from trade_compass_agent.config import AppConfig, load_project_dotenv
from trade_compass_agent.runtime.tools.registry import ToolRegistry
from trade_compass_agent.runtime.tools.search import tool_web_search


@pytest.fixture
def deepseek(monkeypatch):
    monkeypatch.setenv("WEB_SEARCH_PROVIDER", "deepseek")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-search-key")


def search_response(rows):
    response = MagicMock()
    response.json.return_value = {"results": rows}
    return response


def source_row(**overrides):
    return {
        "title": "关于上市公司信息披露的公告",
        "url": "https://www.sse.com.cn/disclosure/example.html",
        "snippet": "公告摘要",
        "summary": "供研究核验的较长摘要。" * 40,
        "content": "",
        "date": "2026-10-20T08:00:00+00:00",
        "site_name": "上海证券交易所",
        "rerank_score": 2.4,
        "authority": 3,
        **overrides,
    }


@pytest.mark.parametrize("provider", [None, "auto", "free", ""])
def test_existing_keys_do_not_enable_paid_search(monkeypatch, provider):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-search-key")
    monkeypatch.setenv("TAVILY_API_KEY", "test-tavily-key")
    if provider is None:
        monkeypatch.delenv("WEB_SEARCH_PROVIDER", raising=False)
    else:
        monkeypatch.setenv("WEB_SEARCH_PROVIDER", provider)
    with (
        patch("trade_compass_agent.runtime.tools.search._web_search_ddg", return_value='{"provider":"brave"}') as free,
        patch("requests.post") as post,
    ):
        assert json.loads(tool_web_search(query="行业政策"))["provider"] == "brave"
    free.assert_called_once()
    post.assert_not_called()


def test_missing_key_is_visible_without_fallback(deepseek, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY")
    with patch("requests.post") as post:
        result = json.loads(tool_web_search(query="行业政策"))
    assert result["provider"] == "deepseek"
    assert "DEEPSEEK_API_KEY" in result["error"]
    assert result["results"] == []
    post.assert_not_called()


def test_registry_preserves_sources_dates_and_provider_scores(deepseek, tmp_path):
    registry = ToolRegistry(SimpleNamespace(config=AppConfig(memory_dir=tmp_path)))
    query = '信息披露 site:sse.com.cn lang:zh after:2026-10-01'
    rows = [source_row(), source_row(url="https://example.com/undated", date=None,
                                  site_name=None, authority=-1, rerank_score=0)]
    with patch("requests.post", return_value=search_response(rows)) as post:
        result = json.loads(registry.execute("web_search", {"query": query, "limit": 2}))
    assert result["provider"] == "deepseek"
    assert result["data_status"] == "available"
    assert result["query"] == query
    assert result["count"] == 2
    assert datetime.fromisoformat(result["fetched_at"]).utcoffset().total_seconds() == 0
    first, undated = result["results"]
    assert {key: first[key] for key in ("title", "url", "snippet", "summary", "site_name", "authority", "rerank_score")} == {
        key: rows[0][key] for key in ("title", "url", "snippet", "summary", "site_name", "authority", "rerank_score")
    }
    assert first["published_date"] == rows[0]["date"]
    assert first["published_date_verified"] is False
    assert not undated.get("published_date")
    assert undated["authority"] == -1 and undated["rerank_score"] == 0
    assert "confidence" not in first
    assert "content" not in first
    assert post.call_args.args == ("https://api.deepseek.com/v1/tools/search",)
    assert post.call_args.kwargs["json"] == {"query": query, "count": 2, "need_content": False}
    assert post.call_args.kwargs["headers"]["Authorization"] == "Bearer test-search-key"


def test_empty_search_is_distinct_from_request_failure(deepseek):
    with patch("requests.post", return_value=search_response([])) as post:
        result = json.loads(tool_web_search(query="不存在的公告"))
    assert result["data_status"] == "empty"
    assert result["count"] == 0 and result["results"] == []
    assert "error" not in result
    post.assert_called_once()


def test_provider_date_is_not_promoted_to_verified_and_long_summary_is_marked(deepseek):
    summary = "发布时间：2024-11-15。" + "政策正文" * 1000
    row = source_row(summary=summary, date="2026-08-25T16:00:00+00:00")
    with patch("requests.post", return_value=search_response([row])):
        result = json.loads(tool_web_search(query="集成电路 政策 after:2026-07-01"))
    item = result["results"][0]
    assert item["published_date"] == row["date"]
    assert item["published_date_verified"] is False
    assert "2024-11-15" in item["summary"]
    assert len(item["summary"]) == 2000 and item["summary_truncated"] is True
    assert item["url"] == row["url"]


@pytest.mark.parametrize("rows", [[{"title": "missing link"}], None, "invalid"])
def test_malformed_results_are_not_reported_as_research_evidence(deepseek, rows):
    with patch("requests.post", return_value=search_response(rows)):
        result = json.loads(tool_web_search(query="行业政策"))
    assert result["data_status"] == "unavailable"
    assert result["error"] and result["results"] == []


@pytest.mark.parametrize("status", [401, 402, 403, 429, 500, 504])
def test_http_failures_are_visible_without_retry_or_secret_leak(deepseek, status):
    response = requests.Response()
    response.status_code = status
    response.url = "https://api.deepseek.com/v1/tools/search"
    response._content = b'{"error":{"message":"test-search-key must not be echoed"}}'
    response.headers["Retry-After"] = "30"
    with patch("requests.post", return_value=response) as post:
        result = json.loads(tool_web_search(query="行业政策"))
    assert result["provider"] == "deepseek"
    assert result["http_status"] == status
    assert str(status) in result["error"]
    assert result["data_status"] == "unavailable" and result["results"] == []
    assert result["retry_after"] == "30"
    assert "test-search-key" not in json.dumps(result)
    post.assert_called_once()


@pytest.mark.parametrize("query", ["", " \t\n", "股" * 129])
def test_invalid_query_does_not_spend_a_request_or_silently_change_filters(deepseek, query):
    with patch("requests.post") as post:
        result = json.loads(tool_web_search(query=query))
    assert result["error"] and result["query"] == query
    post.assert_not_called()


@pytest.mark.parametrize("limit,count", [(0, 1), (100, 50)])
def test_unicode_query_and_count_bounds(deepseek, limit, count):
    with patch("requests.post", return_value=search_response([source_row()])) as post:
        result = json.loads(tool_web_search(query="股" * 128, limit=limit))
    assert "error" not in result
    assert post.call_args.kwargs["json"]["count"] == count


@pytest.mark.parametrize("message", ["bad header test-search-key", "x" * 110 + "test-search-key"])
def test_transport_errors_redact_credentials(deepseek, message):
    with patch("requests.post", side_effect=requests.ConnectionError(message)):
        result = json.loads(tool_web_search(query="行业政策"))
    assert result["error"]
    assert "test-" not in json.dumps(result)


def test_hung_request_returns_within_total_deadline(deepseek):
    from trade_compass_agent.data.network import run_with_timeout

    release = threading.Event()
    with (
        patch("requests.post", side_effect=lambda *a, **k: release.wait(1)),
        patch("trade_compass_agent.runtime.tools.search.run_with_timeout",
              side_effect=lambda fn, timeout, label: run_with_timeout(fn, 0.02, label)) as guard,
    ):
        start = time.monotonic()
        try:
            result = json.loads(tool_web_search(query="行业政策"))
        finally:
            release.set()
    assert time.monotonic() - start < 0.5
    assert guard.call_args.args[1] == 10
    assert "Timeout" in result["error"]
    assert result["data_status"] == "unavailable"


@pytest.mark.parametrize("source_checkout", [True, False])
@pytest.mark.parametrize("fallback", [False, True])
def test_saved_env_selects_search_on_each_start(tmp_path, monkeypatch, source_checkout, fallback):
    monkeypatch.setattr("trade_compass_agent.config.is_source_checkout", lambda: source_checkout)
    monkeypatch.setattr("trade_compass_agent.config.PROJECT_ROOT", tmp_path)
    monkeypatch.setenv("TRADE_COMPASS_HOME", str(tmp_path))
    monkeypatch.delenv("TRADE_COMPASS_ENV_FILE", raising=False)
    provider = "auto" if fallback else "deepseek"
    fallback_provider = "deepseek" if fallback else ""
    (tmp_path / ".env").write_text(
        f"WEB_SEARCH_PROVIDER={provider}\nWEB_SEARCH_FALLBACK_PROVIDER={fallback_provider}\nDEEPSEEK_API_KEY=test-search-key\n"
    )
    for _ in range(2):
        monkeypatch.delenv("WEB_SEARCH_PROVIDER", raising=False)
        monkeypatch.delenv("WEB_SEARCH_FALLBACK_PROVIDER", raising=False)
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        load_project_dotenv()
        with (
            patch("requests.post", return_value=search_response([source_row()])),
            patch("trade_compass_agent.runtime.tools.search._web_search_ddg", return_value='{"error":"free search unavailable"}'),
            patch("trade_compass_agent.runtime.tools.search._web_search_news", side_effect=TimeoutError("news unavailable")),
        ):
            result = json.loads(tool_web_search(query="行业政策"))
        assert result["provider"] == "deepseek" and result["count"] == 1
        assert (result.get("fallback_from") == "free") is fallback


@pytest.mark.parametrize("fallback", [False, True])
def test_agent_receives_sources_and_history_survives_restart(deepseek, tmp_path, monkeypatch, fallback):
    from trade_compass_agent.llm.providers import ChatCompletion, ToolCall
    from trade_compass_agent.runtime.loop import AgentLoop

    class SearchClient:
        def stream_complete(self, messages, **kwargs):
            tool_messages = [m for m in messages if m.role == "tool"]
            if not tool_messages:
                return ChatCompletion(content="", model="test", provider="test", tool_calls=[
                    ToolCall(id="search", name="web_search", arguments='{"query":"信息披露 site:sse.com.cn"}')
                ])
            result = json.loads(tool_messages[-1].content)
            assert result["results"][0]["published_date"] == source_row()["date"]
            assert result["results"][0]["summary"] == source_row()["summary"]
            return ChatCompletion(content=f"研究来源：[公告]({result['results'][0]['url']})。", model="test", provider="test")

    monkeypatch.setattr("trade_compass_agent.runtime.loop.create_chat_client", lambda config: SearchClient())
    monkeypatch.setattr(AgentLoop, "_update_session_summary", lambda *a, **kw: None)
    if fallback:
        monkeypatch.setenv("WEB_SEARCH_PROVIDER", "auto")
        monkeypatch.setenv("WEB_SEARCH_FALLBACK_PROVIDER", "deepseek")
        monkeypatch.setattr("trade_compass_agent.runtime.tools.search._web_search_ddg", lambda **kw: '{"error":"free search unavailable"}')
        monkeypatch.setattr("trade_compass_agent.runtime.tools.search._web_search_news", MagicMock(side_effect=TimeoutError("news unavailable")))
    config = AppConfig(data_dir=tmp_path / "data", memory_dir=tmp_path / "memory", data_provider="sample")
    loop = AgentLoop.from_config(config)
    with patch("requests.post", return_value=search_response([source_row()])) as post:
        result = loop.run_turn("搜索信息披露的官方来源")
    assert source_row()["url"] in result.summary
    assert "数据来源：deepseek" in result.summary
    post.assert_called_once()
    restarted = AgentLoop.from_config(config)
    history = restarted.session_store.load(result.session_id)
    saved = [m for m in history.messages if m.role == "tool" and m.name == "web_search"]
    assert json.loads(saved[0].content)["results"][0]["url"] == source_row()["url"]
    assert (json.loads(saved[0].content).get("fallback_from") == "free") is fallback
