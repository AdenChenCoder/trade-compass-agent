from __future__ import annotations

import json
import socket
from unittest.mock import MagicMock, call, patch

import pytest

from trade_compass_agent.runtime.tools.fetch_url import (
    _HTTPResult,
    _request_once,
    fetch_url_text,
    redact_url_for_display,
    tool_fetch_url,
)


@patch("trade_compass_agent.runtime.tools.fetch_url._resolve_public_ips")
@patch("trade_compass_agent.runtime.tools.fetch_url._request_once")
def test_fetch_url_text_html(mock_request: MagicMock, mock_resolve: MagicMock) -> None:
    mock_resolve.return_value = ("93.184.216.34",)
    mock_request.return_value = _HTTPResult(
        status=200,
        reason="OK",
        headers={"content-type": "text/html; charset=utf-8"},
        body=b"<html><body><p>Hello&nbsp;world</p></body></html>",
    )

    text = fetch_url_text("https://example.com/page")

    assert "Hello world" in text


def test_fetch_url_text_rejects_non_http_scheme() -> None:
    assert "unsupported URL scheme" in fetch_url_text("ftp://example.com/file")


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/admin",
        "http://10.0.0.1/",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1]/",
        "http://[fe80::1]/",
    ],
)
def test_fetch_url_text_blocks_non_public_ip_literals(url: str) -> None:
    assert "non-public IP address" in fetch_url_text(url)


@patch("trade_compass_agent.runtime.tools.fetch_url.socket.getaddrinfo")
def test_fetch_url_text_blocks_hostname_with_any_private_answer(mock_getaddrinfo: MagicMock) -> None:
    mock_getaddrinfo.return_value = [
        (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", 80)),
        (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", 80)),
    ]

    assert "non-public IP address" in fetch_url_text("http://example.com/")


def test_fetch_url_text_rejects_embedded_credentials() -> None:
    assert "embedded credentials" in fetch_url_text("https://user:secret@example.com/")


@patch("trade_compass_agent.runtime.tools.fetch_url._resolve_public_ips")
@patch("trade_compass_agent.runtime.tools.fetch_url._request_once")
def test_fetch_url_text_revalidates_redirects(
    mock_request: MagicMock,
    mock_resolve: MagicMock,
) -> None:
    mock_resolve.side_effect = [("93.184.216.34",), ("142.250.72.14",)]
    mock_request.side_effect = [
        _HTTPResult(
            status=302,
            reason="Found",
            headers={"location": "https://www.example.org/final"},
            body=b"",
        ),
        _HTTPResult(
            status=200,
            reason="OK",
            headers={"content-type": "text/plain"},
            body=b"redirected",
        ),
    ]

    assert fetch_url_text("https://example.com/start") == "redirected"
    assert mock_resolve.call_args_list == [
        call("example.com", 443),
        call("www.example.org", 443),
    ]


@patch("trade_compass_agent.runtime.tools.fetch_url._resolve_public_ips")
@patch("trade_compass_agent.runtime.tools.fetch_url._request_once")
def test_fetch_url_text_blocks_https_downgrade(
    mock_request: MagicMock,
    mock_resolve: MagicMock,
) -> None:
    mock_resolve.return_value = ("93.184.216.34",)
    mock_request.return_value = _HTTPResult(
        status=302,
        reason="Found",
        headers={"location": "http://example.com/insecure"},
        body=b"",
    )

    assert "may not downgrade" in fetch_url_text("https://example.com/start")


@patch("trade_compass_agent.runtime.tools.fetch_url._connection_for_ip")
def test_request_uses_only_validated_ip(mock_connection_for_ip: MagicMock) -> None:
    response = MagicMock()
    response.status = 200
    response.reason = "OK"
    response.getheaders.return_value = [("Content-Type", "text/plain")]
    response.read.return_value = b"ok"
    connection = mock_connection_for_ip.return_value
    connection.getresponse.return_value = response

    parsed = __import__("urllib.parse", fromlist=["urlsplit"]).urlsplit(
        "https://example.com/path"
    )
    result = _request_once(parsed, ("93.184.216.34",), max_bytes=100)

    assert result.body == b"ok"
    mock_connection_for_ip.assert_called_once_with(
        "https",
        "example.com",
        443,
        "93.184.216.34",
    )


@patch("trade_compass_agent.runtime.tools.fetch_url._resolve_public_ips")
@patch("trade_compass_agent.runtime.tools.fetch_url._request_once")
def test_fetch_url_text_caps_response_bytes(
    mock_request: MagicMock,
    mock_resolve: MagicMock,
) -> None:
    mock_resolve.return_value = ("93.184.216.34",)
    mock_request.return_value = _HTTPResult(
        status=200,
        reason="OK",
        headers={"content-type": "text/plain"},
        body=b"12345",
        truncated=True,
    )

    text = fetch_url_text("https://example.com/data", max_bytes=5)

    assert text.startswith("12345")
    assert "内容已截断" in text


def test_redact_url_for_display_removes_credentials_query_values_and_fragment() -> None:
    assert (
        redact_url_for_display("https://user:secret@example.com/path?token=abc&empty=#fragment")
        == "https://example.com/path?token=REDACTED&empty=REDACTED"
    )


@patch("trade_compass_agent.runtime.tools.fetch_url._resolve_public_ips")
@patch("trade_compass_agent.runtime.tools.fetch_url._request_once")
def test_tool_fetch_url_returns_redacted_json(
    mock_request: MagicMock,
    mock_resolve: MagicMock,
) -> None:
    mock_resolve.return_value = ("93.184.216.34",)
    mock_request.return_value = _HTTPResult(
        status=200,
        reason="OK",
        headers={"content-type": "text/plain"},
        body=b"plain body",
    )

    payload = json.loads(tool_fetch_url("https://example.com/data?token=secret"))

    assert payload["url"] == "https://example.com/data?token=REDACTED"
    assert payload["content"] == "plain body"
    assert payload["chars"] == len("plain body")
    assert payload["reader_extract"]["reader_type"] == "webpage_reader"
    assert payload["reader_extract"]["validation_status"] in {"validated", "degraded"}
