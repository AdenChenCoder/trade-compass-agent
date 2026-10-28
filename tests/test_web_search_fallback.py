from __future__ import annotations

import json
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import requests

from trade_compass_agent.config import AppConfig
from trade_compass_agent.llm.providers import ToolCall
from trade_compass_agent.runtime import loop
from trade_compass_agent.runtime.tools.registry import ToolRegistry
from trade_compass_agent.runtime.tools import search


FREE_ERROR = "Web search unavailable: brave: Timeout; duckduckgo: no results"


@pytest.fixture(autouse=True)
def offline_search(monkeypatch):
    monkeypatch.setenv("WEB_SEARCH_PROVIDER", "auto")
    monkeypatch.delenv("WEB_SEARCH_FALLBACK_PROVIDER", raising=False)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-search-key")
    monkeypatch.setenv("TAVILY_API_KEY", "test-tavily-key")
    free = MagicMock(return_value=json.dumps({"query": "政策", "error": FREE_ERROR, "results": []}))
    news = MagicMock(side_effect=TimeoutError("news timeout"))
    response = MagicMock()
    response.json.return_value = {"results": [{
        "title": "官方政策", "url": "https://www.csrc.gov.cn/example.shtml",
        "snippet": "政策摘要", "summary": "政策摘要", "date": "2026-10-20T00:00:00+00:00",
        "site_name": "证监会", "rerank_score": 2.4, "authority": 4,
    }]}
    post = MagicMock(return_value=response)
    monkeypatch.setattr(search, "_web_search_ddg", free)
    monkeypatch.setattr(search, "_web_search_news", news)
    monkeypatch.setattr(requests, "post", post)
    return free, news, post


def test_saved_keys_do_not_enable_fallback(offline_search):
    free, news, post = offline_search
    result = json.loads(search.tool_web_search(query="政策"))
    assert FREE_ERROR in result["error"] and "news search: Timeout" in result["error"]
    assert "fallback_from" not in result
    free.assert_called_once()
    news.assert_called_once()
    post.assert_not_called()


def test_free_success_does_not_spend_a_paid_request(offline_search, monkeypatch):
    free, news, post = offline_search
    monkeypatch.setenv("WEB_SEARCH_FALLBACK_PROVIDER", "deepseek")
    free.return_value = json.dumps({"provider": "brave", "results": [{"url": "https://example.com"}]})
    result = json.loads(search.tool_web_search(query="政策"))
    assert result["provider"] == "brave"
    news.assert_not_called()
    post.assert_not_called()


def test_news_success_is_preserved_without_paid_search(offline_search, monkeypatch):
    _, news, post = offline_search
    monkeypatch.setenv("WEB_SEARCH_FALLBACK_PROVIDER", "deepseek")
    news.side_effect = None
    news.return_value = json.dumps({"provider": "google_news", "data_scope": "news",
                                   "data_status": "fallback", "results": [{"url": "https://example.com"}]})
    result = json.loads(search.tool_web_search(query="政策"))
    assert result["provider"] == "google_news" and result["data_scope"] == "news"
    post.assert_not_called()


@pytest.mark.parametrize("primary", ["auto", "free", ""])
def test_failed_free_chain_uses_deepseek_once_with_provenance(offline_search, monkeypatch, tmp_path, primary):
    free, news, post = offline_search
    monkeypatch.setenv("WEB_SEARCH_PROVIDER", primary)
    monkeypatch.setenv("WEB_SEARCH_FALLBACK_PROVIDER", "deepseek")
    query = "政策 site:csrc.gov.cn after:2026-01-01"
    registry = ToolRegistry(SimpleNamespace(config=AppConfig(memory_dir=tmp_path)))
    result = json.loads(registry.execute("web_search", {"query": query, "limit": 2}))
    assert result["provider"] == "deepseek" and result["data_status"] == "fallback"
    assert result["fallback_from"] == "free"
    assert FREE_ERROR in result["fallback_reason"] and "news search: Timeout" in result["fallback_reason"]
    assert result["count"] == 1 and result["results"][0]["published_date_verified"] is False
    assert result["warnings"]
    assert free.call_args.kwargs["query"] == news.call_args.kwargs["query"] == query
    post.assert_called_once()
    assert post.call_args.kwargs["json"] == {"query": query, "count": 2, "need_content": False}


def test_missing_fallback_key_keeps_both_failure_causes(offline_search, monkeypatch):
    _, _, post = offline_search
    monkeypatch.setenv("WEB_SEARCH_FALLBACK_PROVIDER", "deepseek")
    monkeypatch.delenv("DEEPSEEK_API_KEY")
    result = json.loads(search.tool_web_search(query="政策"))
    assert "DEEPSEEK_API_KEY" in result["error"] and result["data_status"] == "unavailable"
    assert FREE_ERROR in result["fallback_reason"]
    post.assert_not_called()


def test_fallback_rate_limit_is_not_retried_or_hidden(offline_search, monkeypatch):
    _, _, post = offline_search
    monkeypatch.setenv("WEB_SEARCH_FALLBACK_PROVIDER", "deepseek")
    response = requests.Response()
    response.status_code = 429
    response.url = "https://api.deepseek.com/v1/tools/search"
    response.headers["Retry-After"] = "30"
    post.return_value = response
    result = json.loads(search.tool_web_search(query="政策"))
    assert result["http_status"] == 429 and result["retry_after"] == "30"
    assert result["data_status"] == "unavailable" and result["results"] == []
    assert FREE_ERROR in result["fallback_reason"]
    post.assert_called_once()


def test_paid_fallback_with_no_matches_is_empty_not_success_evidence(offline_search, monkeypatch):
    _, _, post = offline_search
    monkeypatch.setenv("WEB_SEARCH_FALLBACK_PROVIDER", "deepseek")
    post.return_value.json.return_value = {"results": []}
    result = json.loads(search.tool_web_search(query="政策"))
    assert result["data_status"] == "empty" and result["count"] == 0
    assert result["fallback_from"] == "free" and result["results"] == []
    assert "warnings" not in result  # No claim that sources were obtained.
    post.assert_called_once()


def test_invalid_fallback_query_preserves_filters_without_paid_request(offline_search, monkeypatch):
    _, _, post = offline_search
    monkeypatch.setenv("WEB_SEARCH_FALLBACK_PROVIDER", "deepseek")
    query = "股" * 120 + " site:csrc.gov.cn"
    result = json.loads(search.tool_web_search(query=query))
    assert result["query"] == query and "128" in result["error"]
    assert result["fallback_from"] == "free"
    post.assert_not_called()


@pytest.mark.parametrize("fallback", ["", "disabled", "tavily"])
def test_disabling_or_mistyping_fallback_never_spends(offline_search, monkeypatch, fallback):
    _, _, post = offline_search
    monkeypatch.setenv("WEB_SEARCH_FALLBACK_PROVIDER", fallback)
    result = json.loads(search.tool_web_search(query="政策"))
    assert FREE_ERROR in result["error"]
    if fallback:
        assert "WEB_SEARCH_FALLBACK_PROVIDER" in result["error"]
    post.assert_not_called()


@pytest.mark.parametrize("primary", ["deepseek", "tavily"])
def test_direct_paid_provider_does_not_enter_free_or_fallback_chain(offline_search, monkeypatch, primary):
    free, news, post = offline_search
    monkeypatch.setenv("WEB_SEARCH_PROVIDER", primary)
    monkeypatch.setenv("WEB_SEARCH_FALLBACK_PROVIDER", "deepseek")
    result = json.loads(search.tool_web_search(query="政策"))
    assert result["provider"] == primary and "fallback_from" not in result
    free.assert_not_called()
    news.assert_not_called()
    post.assert_called_once()


def test_agent_waits_for_paid_fallback_after_free_budget(offline_search, monkeypatch, tmp_path):
    free, _, post = offline_search
    monkeypatch.setenv("WEB_SEARCH_FALLBACK_PROVIDER", "deepseek")
    monkeypatch.setattr(loop, "_PARALLEL_TOOL_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(loop, "_SEARCH_TOOL_TIMEOUT_SECONDS", 0.5)
    payload = free.return_value
    def slow_free(**kwargs):
        time.sleep(0.03)
        return payload
    free.side_effect = slow_free
    registry = ToolRegistry(SimpleNamespace(config=AppConfig(memory_dir=tmp_path)))
    results = loop._execute_tool_calls([ToolCall(id="search", name="web_search", arguments='{"query":"政策"}')], registry, None)
    result = json.loads(results["search"])
    assert result.get("timed_out") is not True
    assert result["provider"] == "deepseek" and result["data_status"] == "fallback"
    post.assert_called_once()
