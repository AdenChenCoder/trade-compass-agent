"""URL fetching tool with improved HTML-to-text extraction."""

from __future__ import annotations

import json
import re
from html import unescape
from urllib.parse import urlparse

_SCRIPT_STYLE_RE = re.compile(r"<(script|style|noscript)[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE)
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"[ \t]+")
_BLANK_LINES_RE = re.compile(r"\n{3,}")
_DEFAULT_MAX_CHARS = 30_000


def fetch_url_text(url: str, *, max_chars: int = _DEFAULT_MAX_CHARS) -> str:
    """Fetch a URL and return extracted text content."""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return f"(unsupported URL scheme: {parsed.scheme})"
    try:
        import httpx
    except ImportError:
        return "(httpx not installed; cannot fetch URL)"
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (compatible; TradeCompassBot/1.0)",
            "Accept": "text/html, text/plain, application/json, */*",
        }
        with httpx.Client(timeout=20.0, follow_redirects=True, max_redirects=5) as client:
            response = client.get(url, headers=headers)
            response.raise_for_status()
            content_type = response.headers.get("content-type", "")
            raw = response.text

            if "json" in content_type.lower():
                return raw[:max_chars]

            if "html" in content_type.lower():
                text = _html_to_text(raw)
            else:
                text = raw

            if len(text) > max_chars:
                text = text[:max_chars] + "\n... [内容已截断]"
            return text or "(empty response)"
    except Exception as exc:
        return f"(failed to fetch URL: {exc})"


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


def tool_fetch_url(url: str) -> str:
    """Tool entry point for the agent to call."""
    text = fetch_url_text(url)
    reader_extract = _safe_reader_extract(url, text)
    return json.dumps(
        {"url": url, "content": text, "chars": len(text), "reader_extract": reader_extract},
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
