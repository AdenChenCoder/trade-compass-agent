from __future__ import annotations

import ipaddress
import os
from collections.abc import Awaitable, Callable
from urllib.parse import urlsplit

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

DEFAULT_MAX_REQUEST_BYTES = 10 * 1024 * 1024


def is_loopback_host(host: str) -> bool:
    """Return whether a bind target is explicitly local to this machine."""
    candidate = host.strip().lower().removeprefix("[").removesuffix("]")
    if candidate == "localhost":
        return True
    try:
        return ipaddress.ip_address(candidate.split("%", 1)[0]).is_loopback
    except ValueError:
        return False


def max_request_bytes() -> int:
    raw = os.getenv("TRADE_COMPASS_MAX_REQUEST_BYTES", "").strip()
    if not raw:
        return DEFAULT_MAX_REQUEST_BYTES
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_MAX_REQUEST_BYTES
    return value if value > 0 else DEFAULT_MAX_REQUEST_BYTES


def host_name_from_header(value: str) -> str:
    candidate = value.strip()
    if candidate.startswith("["):
        closing = candidate.find("]")
        return candidate[1:closing] if closing > 0 else ""
    return candidate.rsplit(":", 1)[0] if candidate.count(":") == 1 else candidate


class TrustedLocalHostMiddleware(BaseHTTPMiddleware):
    """Reject DNS rebinding and non-loopback Host headers."""

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        host = host_name_from_header(request.headers.get("host", ""))
        if host != "testserver" and not is_loopback_host(host):
            return JSONResponse(status_code=400, content={"detail": "Invalid Host header"})
        return await call_next(request)


class LocalOriginMiddleware(BaseHTTPMiddleware):
    """Block cross-site browser writes while allowing CLI and local UI clients."""

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            fetch_site = request.headers.get("sec-fetch-site", "").strip().lower()
            if fetch_site == "cross-site":
                return JSONResponse(status_code=403, content={"detail": "Cross-site request denied"})

            origin = request.headers.get("origin", "").strip()
            if origin:
                try:
                    origin_host = urlsplit(origin).hostname or ""
                except ValueError:
                    origin_host = ""
                if not is_loopback_host(origin_host):
                    return JSONResponse(
                        status_code=403,
                        content={"detail": "Cross-site request denied"},
                    )
        return await call_next(request)


class RequestSizeLimitMiddleware(BaseHTTPMiddleware):
    """Reject declared HTTP request bodies above the configured local limit."""

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        content_length = request.headers.get("content-length", "").strip()
        if content_length:
            try:
                declared_size = int(content_length)
            except ValueError:
                return JSONResponse(status_code=400, content={"detail": "Invalid Content-Length"})
            if declared_size < 0:
                return JSONResponse(status_code=400, content={"detail": "Invalid Content-Length"})
            if declared_size > max_request_bytes():
                return JSONResponse(status_code=413, content={"detail": "Request body too large"})
        return await call_next(request)
