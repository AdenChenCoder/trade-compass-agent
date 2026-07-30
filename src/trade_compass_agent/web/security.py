from __future__ import annotations

import ipaddress
import os
from collections.abc import Awaitable, Callable
from urllib.parse import urlsplit

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

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


class RequestSizeLimitMiddleware:
    """Reject HTTP request bodies above the configured local limit."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope)
        content_length = request.headers.get("content-length", "").strip()
        limit = max_request_bytes()
        if content_length:
            try:
                declared_size = int(content_length)
            except ValueError:
                await JSONResponse(
                    status_code=400,
                    content={"detail": "Invalid Content-Length"},
                )(scope, receive, send)
                return
            if declared_size < 0:
                await JSONResponse(
                    status_code=400,
                    content={"detail": "Invalid Content-Length"},
                )(scope, receive, send)
                return
            if declared_size > limit:
                await JSONResponse(
                    status_code=413,
                    content={"detail": "Request body too large"},
                )(scope, receive, send)
                return

        buffered: list[Message] = []
        received_size = 0
        while True:
            message = await receive()
            buffered.append(message)
            if message["type"] == "http.disconnect":
                break
            if message["type"] != "http.request":
                continue
            received_size += len(message.get("body", b""))
            if received_size > limit:
                await JSONResponse(
                    status_code=413,
                    content={"detail": "Request body too large"},
                )(scope, receive, send)
                return
            if not message.get("more_body", False):
                break

        async def replay_receive() -> Message:
            if buffered:
                return buffered.pop(0)
            return await receive()

        await self.app(scope, replay_receive, send)
