from __future__ import annotations

import os
import re
import threading
import time
from collections.abc import Callable
from typing import TypeVar

T = TypeVar("T")

_EASTMONEY_HOST = re.compile(r"eastmoney\.com", re.I)
_PATCHED = False


# ---------------------------------------------------------------------------
# Per-domain rate limiter — prevents bursting N requests simultaneously
# ---------------------------------------------------------------------------

class _DomainRateLimiter:
    """Per-domain rate limiter: max 1 request per `interval` to each domain.

    Uses per-domain locks so requests to different domains proceed in parallel.
    """

    def __init__(self, interval: float = 0.5):
        self._interval = interval
        self._last: dict[str, float] = {}
        self._meta_lock = threading.Lock()
        self._domain_locks: dict[str, threading.Lock] = {}

    def _get_domain_lock(self, domain: str) -> threading.Lock:
        with self._meta_lock:
            if domain not in self._domain_locks:
                self._domain_locks[domain] = threading.Lock()
            return self._domain_locks[domain]

    def wait(self, domain: str) -> None:
        lock = self._get_domain_lock(domain)
        with lock:
            now = time.monotonic()
            last = self._last.get(domain, 0.0)
            gap = self._interval - (now - last)
            if gap > 0:
                time.sleep(gap)
            self._last[domain] = time.monotonic()


_rate_limiter = _DomainRateLimiter(interval=0.8)


def rate_limit_domain(url: str) -> None:
    """Call before making an HTTP request to auto-throttle per-domain."""
    from urllib.parse import urlparse
    host = urlparse(url).hostname or ""
    base = ".".join(host.rsplit(".", 2)[-2:]) if "." in host else host
    if base:
        _rate_limiter.wait(base)


def short_error_message(exc: BaseException, max_len: int = 120) -> str:
    for attr in ("reason", "message"):
        nested = getattr(exc, attr, None)
        if isinstance(nested, BaseException):
            return short_error_message(nested, max_len=max_len)
        if isinstance(nested, str) and nested.strip():
            return short_error_message(RuntimeError(nested.strip()), max_len=max_len)

    message = str(exc).strip() or exc.__class__.__name__
    message = message.splitlines()[0]
    lowered = message.lower()
    for token in ("RemoteDisconnected", "ConnectionError", "ProxyError", "Timeout"):
        if token in message:
            return token
    if "timed out" in lowered or "timeout" in lowered:
        return "Timeout"
    if len(message) > max_len:
        return message[: max_len - 3] + "..."
    return message


def extend_no_proxy_for_eastmoney() -> None:
    hosts = (
        "eastmoney.com",
        ".eastmoney.com",
        "push2his.eastmoney.com",
        "82.push2.eastmoney.com",
        "quote.eastmoney.com",
    )
    current = os.environ.get("NO_PROXY") or os.environ.get("no_proxy") or ""
    existing = {item.strip() for item in current.split(",") if item.strip()}
    for host in hosts:
        if host not in existing:
            existing.add(host)
    joined = ",".join(sorted(existing))
    os.environ["NO_PROXY"] = joined
    os.environ["no_proxy"] = joined


def patch_requests_for_eastmoney(default_timeout: float = 2.0) -> None:
    """Route East Money HTTP calls around env proxies and enforce connect/read timeouts."""
    global _PATCHED
    if _PATCHED:
        return

    import requests

    original_get = requests.get
    original_session_request = requests.Session.request

    def _eastmoney_kwargs(url: str, kwargs: dict) -> dict:
        if not _EASTMONEY_HOST.search(str(url)):
            return kwargs
        patched = dict(kwargs)
        if patched.get("timeout") is None:
            patched["timeout"] = default_timeout
        patched.setdefault("proxies", {"http": None, "https": None})
        return patched

    def patched_get(url: str, *args, **kwargs):
        return original_get(url, *args, **_eastmoney_kwargs(url, kwargs))

    def patched_session_request(self, method: str, url: str, *args, **kwargs):
        return original_session_request(self, method, url, *args, **_eastmoney_kwargs(url, kwargs))

    requests.get = patched_get  # type: ignore[method-assign]
    requests.Session.request = patched_session_request  # type: ignore[method-assign]
    _PATCHED = True


def patch_requests_default_timeout(default_timeout: float = 8.0) -> None:
    """Ensure all requests.get/Session.request calls have a timeout if none was set."""
    import requests

    _original_get = requests.get.__wrapped__ if hasattr(requests.get, "__wrapped__") else requests.get
    _original_session_request = (
        requests.Session.request.__wrapped__
        if hasattr(requests.Session.request, "__wrapped__")
        else requests.Session.request
    )

    def _patched_get(url, *args, **kwargs):
        kwargs.setdefault("timeout", default_timeout)
        return _original_get(url, *args, **kwargs)

    def _patched_session_request(self, method, url, *args, **kwargs):
        kwargs.setdefault("timeout", default_timeout)
        return _original_session_request(self, method, url, *args, **kwargs)

    _patched_get.__wrapped__ = _original_get  # type: ignore[attr-defined]
    _patched_session_request.__wrapped__ = _original_session_request  # type: ignore[attr-defined]
    requests.get = _patched_get  # type: ignore[assignment]
    requests.Session.request = _patched_session_request  # type: ignore[assignment]


def run_with_timeout(func: Callable[[], T], timeout: float, description: str) -> T:
    """Run func in a daemon thread with a hard timeout.

    Uses shutdown(wait=False) so the caller never blocks on a hung thread.
    """
    if timeout <= 0:
        return func()
    import queue
    import socket

    outcome: queue.Queue[tuple[bool, T | BaseException]] = queue.Queue(maxsize=1)

    def _guarded():
        old = socket.getdefaulttimeout()
        socket.setdefaulttimeout(timeout)
        try:
            outcome.put((True, func()))
        except BaseException as exc:
            outcome.put((False, exc))
        finally:
            socket.setdefaulttimeout(old)

    worker = threading.Thread(
        target=_guarded,
        daemon=True,
        name=f"timeout:{description}",
    )
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        raise TimeoutError(f"{description} timed out after {timeout:.0f}s")
    succeeded, value = outcome.get_nowait()
    if succeeded:
        return value  # type: ignore[return-value]
    raise value  # type: ignore[misc]
