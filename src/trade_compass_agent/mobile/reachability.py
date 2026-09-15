"""Bounded, anonymous checks of this computer's own public mobile entry.

These observations never control the listener, node identity or phone authorization.
"""
import asyncio
from contextlib import suppress
import ipaddress
import json
import re
import socket
import ssl
import time
from urllib.parse import urlsplit

CHECK_INTERVAL = 60
MAX_AGE = 90
CONNECT_TIMEOUT = 8
MAX_ADDRESSES = 8

MESSAGES = {
    "checking": "正在从这台电脑检测公网入口…",
    "reachable": "已检测的公网路径均可访问。",
    "partial": "部分公网路径未连通，手机访问可能时好时坏。",
    "unreachable": "这台电脑暂未连通公网入口。请检查电脑网络，稍后会自动重试。",
    "inconclusive": "这台电脑暂未完成公网检测，稍后会自动重试。",
    "stale": "上次检测已过期，正在等待新的检测结果。",
}


async def _probe_address(host, address, origin, computer_id, context):
    writer = None
    try:
        async with asyncio.timeout(CONNECT_TIMEOUT):
            # Numeric IP prevents a second DNS lookup; SNI/Host and CA validation
            # still use the original domain. No proxies, credentials or redirects.
            reader, writer = await asyncio.open_connection(
                address, 443, ssl=context, server_hostname=host, limit=4096)
            writer.write((f"GET /mobile/v1/browser/connection HTTP/1.1\r\nHost: {host}\r\n"
                          "Connection: close\r\nAccept: application/json\r\n\r\n").encode("ascii"))
            await writer.drain()
            head = (await reader.readuntil(b"\r\n\r\n")).decode("ascii")
            lines = head.split("\r\n")
            if lines[0].split()[:2] != ["HTTP/1.1", "200"]:
                return False
            headers = {}
            for line in lines[1:-2]:
                key, value = line.split(":", 1)
                key = key.lower()
                # Both TLS proxy and application add security/cache headers.
                # Only ambiguous message framing prevents a bounded body read.
                if key in headers and key in {"content-length", "transfer-encoding"}:
                    return False
                headers[key] = value.strip()
            size = int(headers.get("content-length", "0"))
            if not 0 < size <= 1024 or "transfer-encoding" in headers:
                return False
            value = json.loads(await reader.readexactly(size))
            return (isinstance(value, dict) and value.get("connected") is False
                    and value.get("endpoint") == origin and value.get("computer_id") == computer_id)
    except (OSError, ValueError, asyncio.TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
        return False
    finally:
        if writer is not None:
            # Abort after the bounded response; do not wait indefinitely for TLS shutdown.
            writer.transport.abort()


async def check_public_entry(origin, computer_id):
    """Resolve once and check every bounded public DNS answer, with system trust."""
    empty = {"state": "inconclusive", "checked": 0, "reachable": 0}
    if not isinstance(origin, str) or not re.fullmatch(r"https://[a-z0-9-]+\.[a-z0-9.-]+\.ts\.net", origin):
        return empty
    try:
        host = urlsplit(origin).hostname
        answers = await asyncio.wait_for(asyncio.get_running_loop().getaddrinfo(
            host, 443, type=socket.SOCK_STREAM), timeout=5)
        addresses = sorted({answer[4][0] for answer in answers})
        ips = [ipaddress.ip_address(address) for address in addresses]
        if (not addresses or len(addresses) > MAX_ADDRESSES
                or any(not ip.is_global or ip.is_multicast for ip in ips)):
            return empty
        context = ssl.create_default_context()
        results = await asyncio.gather(*(
            _probe_address(host, address, origin, computer_id, context) for address in addresses))
        successes = sum(results)
        return {"state": "reachable" if successes == len(results) else "partial" if successes else "unreachable",
                "checked": len(results), "reachable": successes}
    except (OSError, ValueError, asyncio.TimeoutError):
        return empty


class PublicReachability:
    def __init__(self, origin, computer_id):
        self.origin, self.computer_id = origin, computer_id
        self.result = {"state": "checking", "checked": 0, "reachable": 0, "checked_at": None}
        self.task = asyncio.create_task(self._run(), name="mobile-public-check")

    def status(self):
        value = dict(self.result)
        checked_at = value["checked_at"]
        if checked_at is not None and not 0 <= time.time() - checked_at <= MAX_AGE:
            value.update(state="stale", checked=0, reachable=0)
        return {**value, "scope": "computer", "message": MESSAGES[value["state"]]}

    async def _run(self):
        while True:
            started = time.time()
            try:
                result = await check_public_entry(self.origin, self.computer_id)
            except Exception:
                # A diagnostic failure must never stop mobile access or its owner.
                result = {"state": "inconclusive", "checked": 0, "reachable": 0}
            self.result = {**result, "checked_at": started}
            await asyncio.sleep(CHECK_INTERVAL)

    async def close(self):
        self.task.cancel()
        with suppress(asyncio.CancelledError):
            await self.task
