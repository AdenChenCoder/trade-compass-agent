"""Bounded anonymous TLS probes; target metadata must never enter public logs."""
from __future__ import annotations

import http.client
import ipaddress
import json
import os
import re
import socket
import ssl
import time
from urllib.parse import urlsplit


def target_from(raw):
    value = json.loads(raw)
    origin = urlsplit(value["origin"])
    if (origin.scheme != "https" or origin.username or origin.password
            or origin.port not in (None, 443) or origin.path not in ("", "/")
            or origin.query or origin.fragment
            or not re.fullmatch(r"[a-z0-9-]+\.[a-z0-9.-]+\.ts\.net", origin.hostname or "")):
        raise ValueError("invalid target")
    addresses = value["ips"]
    if (not isinstance(addresses, list) or not 1 <= len(addresses) <= 8
            or not all(isinstance(ip, str) and ipaddress.ip_address(ip).is_global for ip in addresses)
            or not isinstance(value["computer_id"], str) or not value["computer_id"]):
        raise ValueError("invalid target")
    return origin.hostname, addresses, value["computer_id"]


def probe(host, address, computer_id):
    result = {"at": time.time(), "stage": "tcp", "ok": False}
    try:
        with socket.create_connection((address, 443), timeout=5) as connection:
            result["stage"] = "tls"
            with ssl.create_default_context().wrap_socket(connection, server_hostname=host) as secured:
                result["stage"] = "http"
                client = http.client.HTTPConnection(host, timeout=5)
                client.sock = secured
                try:
                    client.request("GET", "/mobile/v1/browser/connection", headers={"Connection": "close"})
                    response = client.getresponse()
                    result["status"] = response.status
                    body = response.read(8193)
                    result["stage"] = "identity"
                    if response.status == 200 and len(body) <= 8192:
                        result["ok"] = json.loads(body).get("computer_id") == computer_id
                    if result["ok"]:
                        result["stage"] = "complete"
                finally:
                    client.close()
    except Exception:
        # No provider exception strings: they can include the private target.
        pass
    return result


def main():
    try:
        host, addresses, computer_id = target_from(os.environ["COMPASS_TLS_PROBE_TARGET"])
    except Exception:
        print(json.dumps({"ok": False, "stage": "target_configuration"}), flush=True)
        return 2
    started = time.monotonic()
    all_ok = True
    for cycle in range(6):
        remaining = started + cycle * 20 - time.monotonic()
        if remaining > 0:
            time.sleep(remaining)
        for index, address in enumerate(addresses):
            if time.monotonic() - started > 135:
                print(json.dumps({"ok": False, "stage": "probe_deadline"}), flush=True)
                return 2
            result = probe(host, address, computer_id)
            all_ok = all_ok and result["ok"]
            result.update(cycle=cycle, path=index)
            print(json.dumps(result), flush=True)
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
