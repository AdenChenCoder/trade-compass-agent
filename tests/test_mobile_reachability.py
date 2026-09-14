import asyncio
import json
import socket
import ssl

import pytest

from trade_compass_agent.mobile import reachability as implementation
from test_mobile_managed import managed as managed

ORIGIN = "https://computer.test.ts.net"


@pytest.mark.parametrize("mode,expected", [
    ("valid", True), ("wrong_identity", False), ("wrong_origin", False),
    ("redirect", False), ("oversized", False), ("ambiguous_length", False), ("untrusted", False),
    ("wrong_certificate_name", False), ("timeout", False),
])
def test_real_tls_probe_checks_identity_and_keeps_security_boundaries(managed, monkeypatch, mode, expected):
    _, _, state, _ = managed
    certs = state / "tsnet/certs"
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(certs / "computer.test.ts.net.crt", certs / "computer.test.ts.net.key")
    names, requests = [], []
    server_context.set_servername_callback(lambda _socket, name, _context: names.append(name))
    context = ssl.create_default_context()
    if mode != "untrusted":
        context.load_verify_locations(certs / "computer.test.ts.net.crt")
    assert context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED
    monkeypatch.setattr(implementation, "CONNECT_TIMEOUT", .3)

    async def run():
        async def serve(reader, writer):
            try:
                requests.append(await reader.readuntil(b"\r\n\r\n"))
                if mode == "timeout":
                    await reader.read()  # Client must terminate without a response.
                    return
                value = {"connected": False, "endpoint": ORIGIN, "computer_id": "original-computer"}
                if mode == "wrong_identity":
                    value["computer_id"] = "another-computer"
                if mode == "wrong_origin":
                    value["endpoint"] = "https://another.test.ts.net"
                body = json.dumps(value).encode()
                response = (b"HTTP/1.1 200 OK\r\nCache-Control: no-store\r\nCache-Control: no-store\r\n"
                            b"X-Frame-Options: DENY\r\nX-Frame-Options: DENY\r\nContent-Length: "
                            + str(len(body)).encode() + b"\r\n\r\n" + body)
                if mode == "ambiguous_length":
                    response = b"HTTP/1.1 200 OK\r\nContent-Length: 1\r\nContent-Length: 100\r\n\r\n"
                if mode == "redirect":
                    response = b"HTTP/1.1 302 Found\r\nLocation: http://127.0.0.1/private\r\nContent-Length: 0\r\n\r\n"
                if mode == "oversized":
                    response = b"HTTP/1.1 200 OK\r\nContent-Length: 9999999\r\n\r\n"
                writer.write(response)
                await writer.drain()
            finally:
                writer.close()

        async with await asyncio.start_server(serve, "127.0.0.1", 0, ssl=server_context) as server:
            real_connect = asyncio.open_connection

            async def local_connect(address, port, **kwargs):
                assert address == "8.8.8.8" and port == 443
                return await real_connect("127.0.0.1", server.sockets[0].getsockname()[1], **kwargs)

            monkeypatch.setattr(asyncio, "open_connection", local_connect)
            host = "wrong.test.ts.net" if mode == "wrong_certificate_name" else "computer.test.ts.net"
            assert await implementation._probe_address(host, "8.8.8.8", ORIGIN, "original-computer", context) is expected
            await asyncio.sleep(0)
            assert names == [host]
            assert len(requests) == (0 if mode in {"untrusted", "wrong_certificate_name"} else 1)
            if requests:
                assert requests[0] == (b"GET /mobile/v1/browser/connection HTTP/1.1\r\n"
                                       b"Host: computer.test.ts.net\r\nConnection: close\r\n"
                                       b"Accept: application/json\r\n\r\n")

    asyncio.run(run())


@pytest.mark.parametrize("outcomes,expected", [([True, True], "reachable"), ([True, False], "partial"), ([False, False], "unreachable")])
def test_every_resolved_path_contributes_to_computer_scoped_result(monkeypatch, outcomes, expected):
    async def run():
        calls = []
        addresses = ["8.8.8.8", "2606:4700:4700::1111"]

        async def resolve(host, port, **kwargs):
            assert host == "computer.test.ts.net" and port == 443 and kwargs == {"type": socket.SOCK_STREAM}
            return [(0, 0, 0, "", (ip, 443)) for ip in addresses + addresses]

        async def probe(host, ip, origin, computer, context):
            assert origin == ORIGIN and computer == "same-computer"
            assert context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED
            calls.append(ip)
            return outcomes[addresses.index(ip)]

        monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolve)
        monkeypatch.setattr(implementation, "_probe_address", probe)
        assert await implementation.check_public_entry(ORIGIN, "same-computer") == {
            "state": expected, "checked": 2, "reachable": sum(outcomes)}
        assert sorted(calls) == sorted(addresses)

    asyncio.run(run())


@pytest.mark.parametrize("addresses", [["127.0.0.1"], ["::1"], ["224.0.0.1"], ["ff0e::1"], ["8.8.8.8", "192.168.1.1"], [], [f"8.8.8.{i}" for i in range(1, 10)]])
def test_invalid_or_excessive_dns_answers_never_open_connections(monkeypatch, addresses):
    async def run():
        async def resolve(*_args, **_kwargs):
            return [(0, 0, 0, "", (ip, 443)) for ip in addresses]

        async def forbidden(*_args, **_kwargs):
            pytest.fail("invalid DNS answers must not cause network access")

        monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolve)
        monkeypatch.setattr(implementation, "_probe_address", forbidden)
        assert (await implementation.check_public_entry(ORIGIN, "same-computer"))["state"] == "inconclusive"

    asyncio.run(run())


def test_monitor_expires_old_results_recovers_and_cancels_without_persistence(monkeypatch):
    async def run():
        async def check(*_args):
            return {"state": "reachable", "checked": 2, "reachable": 2}

        monkeypatch.setattr(implementation, "check_public_entry", check)
        monkeypatch.setattr(implementation, "CHECK_INTERVAL", .08)
        monkeypatch.setattr(implementation, "MAX_AGE", .03)
        monitor = implementation.PublicReachability(ORIGIN, "computer")
        assert monitor.status()["state"] == "checking"
        await asyncio.sleep(0)
        assert monitor.status()["state"] == "reachable"
        assert monitor.status()["scope"] == "computer"
        await asyncio.sleep(.04)
        assert monitor.status()["state"] == "stale" and monitor.status()["checked"] == 0
        await asyncio.sleep(.05)
        assert monitor.status()["state"] == "reachable"
        await monitor.close()
        assert monitor.task.done()

    asyncio.run(run())
