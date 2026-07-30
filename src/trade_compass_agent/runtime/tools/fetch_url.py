"""Safely fetch public HTTP(S) URLs and extract readable text."""

from __future__ import annotations

import http.client
import ipaddress
import json
import re
import socket
import ssl
from dataclasses import dataclass
from html import unescape
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

_SCRIPT_STYLE_RE = re.compile(r"<(script|style|noscript)[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE)
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"[ \t]+")
_BLANK_LINES_RE = re.compile(r"\n{3,}")
_CHARSET_RE = re.compile(r"charset\s*=\s*[\"']?([^;\"'\s]+)", re.IGNORECASE)
_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_DEFAULT_MAX_CHARS = 30_000
_DEFAULT_MAX_BYTES = 1_000_000
_DEFAULT_TIMEOUT_SECONDS = 20.0
_DEFAULT_MAX_REDIRECTS = 5


class _BlockedURL(ValueError):
    """Raised when a URL could reach a non-public network destination."""


@dataclass(frozen=True)
class _HTTPResult:
    status: int
    reason: str
    headers: dict[str, str]
    body: bytes
    truncated: bool = False


class _PinnedHTTPConnection(http.client.HTTPConnection):
    """HTTP connection that uses a previously validated destination IP."""

    def __init__(self, host: str, port: int, connect_ip: str, *, timeout: float) -> None:
        super().__init__(host, port=port, timeout=timeout)
        self._connect_ip = connect_ip

    def connect(self) -> None:
        self.sock = socket.create_connection(
            (self._connect_ip, self.port),
            self.timeout,
            self.source_address,
        )


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS connection pinned to an IP while retaining hostname TLS checks."""

    def __init__(self, host: str, port: int, connect_ip: str, *, timeout: float) -> None:
        super().__init__(
            host,
            port=port,
            timeout=timeout,
            context=ssl.create_default_context(),
        )
        self._connect_ip = connect_ip

    def connect(self) -> None:
        raw_socket = socket.create_connection(
            (self._connect_ip, self.port),
            self.timeout,
            self.source_address,
        )
        try:
            self.sock = self._context.wrap_socket(raw_socket, server_hostname=self.host)
        except Exception:
            raw_socket.close()
            raise


def fetch_url_text(
    url: str,
    *,
    max_chars: int = _DEFAULT_MAX_CHARS,
    max_bytes: int = _DEFAULT_MAX_BYTES,
) -> str:
    """Fetch a public URL and return extracted text content."""
    if max_chars <= 0 or max_bytes <= 0:
        return "(failed to fetch URL: response limits must be positive)"
    try:
        result = _fetch_with_redirects(url, max_bytes=max_bytes)
        if result.status >= 400:
            return f"(failed to fetch URL: HTTP {result.status} {result.reason})"

        content_type = result.headers.get("content-type", "")
        raw = _decode_body(result.body, content_type)
        if "json" in content_type.lower():
            text = raw
        elif "html" in content_type.lower():
            text = _html_to_text(raw)
        else:
            text = raw

        was_truncated = result.truncated or len(text) > max_chars
        if len(text) > max_chars:
            text = text[:max_chars]
        if was_truncated:
            text += "\n... [内容已截断]"
        return text or "(empty response)"
    except _BlockedURL as exc:
        return f"(blocked URL: {exc})"
    except Exception as exc:
        return f"(failed to fetch URL: {exc})"


def _fetch_with_redirects(url: str, *, max_bytes: int) -> _HTTPResult:
    current_url = url
    for redirect_count in range(_DEFAULT_MAX_REDIRECTS + 1):
        parsed, addresses = _validate_and_resolve(current_url)
        result = _request_once(parsed, addresses, max_bytes=max_bytes)
        location = result.headers.get("location")
        if result.status not in _REDIRECT_STATUSES or not location:
            return result
        if redirect_count >= _DEFAULT_MAX_REDIRECTS:
            raise _BlockedURL("too many redirects")
        next_url = urljoin(current_url, location)
        next_parsed = urlsplit(next_url)
        if parsed.scheme == "https" and next_parsed.scheme == "http":
            raise _BlockedURL("HTTPS redirects may not downgrade to HTTP")
        current_url = next_url
    raise _BlockedURL("too many redirects")


def _validate_and_resolve(url: str):
    if not url or any(ord(char) < 32 or ord(char) == 127 for char in url):
        raise _BlockedURL("URL is empty or contains control characters")
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"}:
        raise _BlockedURL(f"unsupported URL scheme: {parsed.scheme}")
    if parsed.username is not None or parsed.password is not None:
        raise _BlockedURL("embedded credentials are not allowed")
    hostname = parsed.hostname
    if not hostname:
        raise _BlockedURL("URL has no hostname")
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError as exc:
        raise _BlockedURL("URL has an invalid port") from exc
    addresses = _resolve_public_ips(hostname, port)
    return parsed, addresses


def _resolve_public_ips(hostname: str, port: int) -> tuple[str, ...]:
    try:
        direct_ip = ipaddress.ip_address(hostname.rstrip("."))
    except ValueError:
        direct_ip = None

    if direct_ip is not None:
        addresses = (str(direct_ip),)
    else:
        try:
            resolved = socket.getaddrinfo(
                hostname,
                port,
                type=socket.SOCK_STREAM,
                proto=socket.IPPROTO_TCP,
            )
        except socket.gaierror as exc:
            raise _BlockedURL(f"hostname could not be resolved: {hostname}") from exc
        addresses = tuple(dict.fromkeys(item[4][0] for item in resolved))

    if not addresses:
        raise _BlockedURL(f"hostname could not be resolved: {hostname}")
    unsafe = [address for address in addresses if not ipaddress.ip_address(address).is_global]
    if unsafe:
        raise _BlockedURL("destination resolves to a non-public IP address")
    return addresses


def _request_once(parsed, addresses: tuple[str, ...], *, max_bytes: int) -> _HTTPResult:
    hostname = parsed.hostname
    if hostname is None:
        raise _BlockedURL("URL has no hostname")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    target = parsed.path or "/"
    if parsed.query:
        target += f"?{parsed.query}"
    default_port = 443 if parsed.scheme == "https" else 80
    host_header = hostname if port == default_port else f"{hostname}:{port}"
    if ":" in hostname and not hostname.startswith("["):
        host_header = f"[{hostname}]" if port == default_port else f"[{hostname}]:{port}"
    headers = {
        "Host": host_header,
        "User-Agent": "Mozilla/5.0 (compatible; TradeCompassBot/1.0)",
        "Accept": "text/html, text/plain, application/json, */*",
        "Accept-Encoding": "identity",
        "Connection": "close",
    }

    last_error: Exception | None = None
    for address in addresses:
        connection = _connection_for_ip(parsed.scheme, hostname, port, address)
        try:
            connection.request("GET", target, headers=headers)
            response = connection.getresponse()
            body = response.read(max_bytes + 1)
            return _HTTPResult(
                status=response.status,
                reason=response.reason or "",
                headers={key.lower(): value for key, value in response.getheaders()},
                body=body[:max_bytes],
                truncated=len(body) > max_bytes,
            )
        except (OSError, http.client.HTTPException, ssl.SSLError) as exc:
            last_error = exc
        finally:
            connection.close()
    if last_error is not None:
        raise last_error
    raise OSError("no validated destination address was available")


def _connection_for_ip(
    scheme: str,
    hostname: str,
    port: int,
    address: str,
) -> http.client.HTTPConnection:
    if scheme == "https":
        return _PinnedHTTPSConnection(
            hostname,
            port,
            address,
            timeout=_DEFAULT_TIMEOUT_SECONDS,
        )
    return _PinnedHTTPConnection(
        hostname,
        port,
        address,
        timeout=_DEFAULT_TIMEOUT_SECONDS,
    )


def _decode_body(body: bytes, content_type: str) -> str:
    match = _CHARSET_RE.search(content_type)
    charset = match.group(1) if match else "utf-8"
    try:
        return body.decode(charset, errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")


def _html_to_text(html: str) -> str:
    """Convert HTML to readable text, stripping scripts/styles/tags."""
    text = _SCRIPT_STYLE_RE.sub("", html)
    text = unescape(text)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</(p|div|h[1-6]|li|tr|section|article)>", "\n\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<(h[1-6])[^>]*>", "\n\n## ", text, flags=re.IGNORECASE)
    text = re.sub(r"<li[^>]*>", "\n- ", text, flags=re.IGNORECASE)
    text = _HTML_TAG_RE.sub("", text)
    text = text.replace("\xa0", " ")
    text = _WHITESPACE_RE.sub(" ", text)
    text = _BLANK_LINES_RE.sub("\n\n", text)
    return text.strip()


def redact_url_for_display(url: str) -> str:
    """Remove credentials, fragments, and query values before persistence/display."""
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname or ""
        if ":" in hostname and not hostname.startswith("["):
            hostname = f"[{hostname}]"
        port = f":{parsed.port}" if parsed.port is not None else ""
        query = urlencode(
            [(key, "REDACTED") for key, _ in parse_qsl(parsed.query, keep_blank_values=True)]
        )
        return urlunsplit((parsed.scheme, f"{hostname}{port}", parsed.path, query, ""))
    except ValueError:
        return "(invalid URL)"


def tool_fetch_url(url: str) -> str:
    """Tool entry point for the agent to call."""
    text = fetch_url_text(url)
    display_url = redact_url_for_display(url)
    reader_extract = _safe_reader_extract(display_url, text)
    return json.dumps(
        {
            "url": display_url,
            "content": text,
            "chars": len(text),
            "reader_extract": reader_extract,
        },
        ensure_ascii=False,
    )


def _safe_reader_extract(url: str, text: str) -> dict:
    from trade_compass_agent.config import resolve_schema_path
    from trade_compass_agent.runtime.readers import ReaderInput, read_untrusted_text
    from trade_compass_agent.runtime.schema_validator import SchemaValidationError, validate_schema

    result = read_untrusted_text(
        ReaderInput(
            reader_type="webpage_reader",
            content=text,
            source=f"fetch_url:{url}",
            source_url=url,
        )
    ).model_dump()
    schema_path = resolve_schema_path("readers/reader_claims.schema.json")
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    try:
        validate_schema(result, schema)
    except SchemaValidationError as exc:
        result["claims"] = []
        result["events"] = []
        result["confidence"] = "low"
        result["validation_status"] = "degraded"
        result["warnings"] = [*result.get("warnings", []), f"schema validation failed: {exc}"]
        validate_schema(result, schema)
    return result
