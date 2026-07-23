from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from trade_compass_agent.runtime.tools.fetch_url import fetch_url_text, tool_fetch_url


@patch("httpx.Client")
def test_fetch_url_text_html(mock_client_cls: MagicMock) -> None:
    mock_response = MagicMock()
    mock_response.headers = {"content-type": "text/html; charset=utf-8"}
    mock_response.text = "<html><body><p>Hello&nbsp;world</p></body></html>"
    mock_response.raise_for_status = MagicMock()
    mock_client_cls.return_value.__enter__.return_value.get.return_value = mock_response

    text = fetch_url_text("https://example.com/page")
    assert "Hello world" in text


def test_fetch_url_text_rejects_non_http_scheme() -> None:
    assert "unsupported URL scheme" in fetch_url_text("ftp://example.com/file")


@patch("httpx.Client")
def test_tool_fetch_url_returns_json(mock_client_cls: MagicMock) -> None:
    mock_response = MagicMock()
    mock_response.headers = {"content-type": "text/plain"}
    mock_response.text = "plain body"
    mock_response.raise_for_status = MagicMock()
    mock_client_cls.return_value.__enter__.return_value.get.return_value = mock_response

    payload = json.loads(tool_fetch_url("https://example.com/data"))
    assert payload["url"] == "https://example.com/data"
    assert payload["content"] == "plain body"
    assert payload["chars"] == len("plain body")
    assert payload["reader_extract"]["reader_type"] == "webpage_reader"
    assert payload["reader_extract"]["validation_status"] in {"validated", "degraded"}
