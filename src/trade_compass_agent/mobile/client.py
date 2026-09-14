"""Executable protocol reference for native clients; no browser trust exceptions needed."""
from __future__ import annotations

import hashlib
import hmac
import http.client
import json
import re
import ssl
from urllib.parse import urlsplit


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, port: int, fingerprint: str, *, timeout: float = 10):
        if not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
            raise ValueError("Expected a SHA-256 certificate fingerprint from the computer")
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        # Trust is the exact certificate obtained out of band, not a public CA or DNS name.
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        super().__init__(host, port, context=context, timeout=timeout)
        self.fingerprint = fingerprint

    def connect(self) -> None:
        super().connect()
        actual = hashlib.sha256(self.sock.getpeercert(binary_form=True)).hexdigest()
        if not hmac.compare_digest(actual, self.fingerprint):
            self.close()
            raise ssl.SSLCertVerificationError("Computer certificate fingerprint does not match")


def request_json(endpoint: str, fingerprint: str, method: str, path: str,
                 *, secret: str | None = None, body: dict | None = None) -> tuple[int, dict | list]:
    url = urlsplit(endpoint)
    if (url.scheme != "https" or not url.hostname or url.username or url.password
            or url.path not in {"", "/"} or url.query or url.fragment):
        raise ValueError("Endpoint must be an HTTPS origin without credentials, path, or query")
    if not path.startswith("/mobile/v1/") or "\r" in path or "\n" in path:
        raise ValueError("Expected a mobile protocol path")
    connection = PinnedHTTPSConnection(url.hostname, url.port or 443, fingerprint)
    headers = {"Content-Type": "application/json"}
    if secret:
        headers["Authorization"] = f"Bearer {secret}"
    try:
        # connect() verifies the actual socket before http.client sends headers or a body.
        connection.request(method, path, body=json.dumps(body) if body is not None else None,
                           headers=headers)
        response = connection.getresponse()
        data = response.read(16 * 1024 * 1024 + 1)
        if len(data) > 16 * 1024 * 1024:
            raise ValueError("Response too large; request a smaller page")
        return response.status, json.loads(data)
    finally:
        connection.close()
