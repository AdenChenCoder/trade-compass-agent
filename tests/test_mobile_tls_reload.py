import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import http.client
import json
from pathlib import Path
import secrets
import socket
import ssl

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
import pytest

from trade_compass_agent.config import AppConfig, MobileConfig
from trade_compass_agent.mobile.api import COOKIE, local_status
from trade_compass_agent.mobile.server import mobile_listener
from trade_compass_agent.runtime.session import SessionMessageRecord, SessionStore

ORIGIN = "https://computer.example"


@pytest.fixture
def certificates(tmp_path, monkeypatch):
    monkeypatch.setattr("trade_compass_agent.mobile.tls_reload.RELOAD_SECONDS", 0.03)
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Isolated test CA")])
    now = datetime.now(timezone.utc)
    ca = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
          .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(days=1))
          .not_valid_after(now + timedelta(days=2))
          .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
          .add_extension(x509.KeyUsage(False, False, False, False, False, True, True, False, False), critical=True)
          .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
          .sign(key, hashes.SHA256()))
    trust = ssl.create_default_context(cadata=ca.public_bytes(serialization.Encoding.PEM).decode())

    def issue(host="computer.example", expired=False):
        leaf_key = ec.generate_private_key(ec.SECP256R1())
        leaf = (x509.CertificateBuilder()
                .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host)]))
                .issuer_name(name).public_key(leaf_key.public_key())
                .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(days=1))
                .not_valid_after(now + timedelta(days=-0.5 if expired else 1))
                .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
                .add_extension(x509.SubjectAlternativeName([x509.DNSName(host)]), critical=False)
                .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
                .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(key.public_key()), critical=False)
                .sign(key, hashes.SHA256()))
        return (leaf.public_bytes(serialization.Encoding.PEM),
                leaf_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                       serialization.NoEncryption()),
                leaf.fingerprint(hashes.SHA256()).hex())

    certfile, keyfile = tmp_path / "public.crt", tmp_path / "public.key"
    original = issue()
    certfile.write_bytes(original[0])
    keyfile.write_bytes(original[1])
    config = AppConfig(data_dir=tmp_path / "data", memory_dir=tmp_path / "memory",
                       mobile=MobileConfig(True, "127.0.0.1", 0, ORIGIN,
                                           str(certfile), str(keyfile), tls_relay=True))
    monkeypatch.setenv("TRADE_COMPASS_DATA_DIR", str(config.data_dir))
    monkeypatch.setenv("TRADE_COMPASS_MEMORY_DIR", str(config.memory_dir))
    return config, trust, issue, original


def request(service, trust, secret=None):
    with socket.create_connection(("127.0.0.1", service.port), timeout=2) as sock:
        with trust.wrap_socket(sock, server_hostname="computer.example") as tls:
            fingerprint = hashlib.sha256(tls.getpeercert(binary_form=True)).hexdigest()
            cookie = f"Cookie: {COOKIE}={secret}\r\n" if secret else ""
            tls.sendall(("GET /mobile/v1/sessions HTTP/1.1\r\nHost: computer.example\r\n"
                         + cookie + "Connection: close\r\n\r\n").encode())
            response = http.client.HTTPResponse(tls)
            response.begin()
            return response.status, json.loads(response.read()), fingerprint


async def until_rotated(service, fingerprint):
    async with asyncio.timeout(3):
        while service.identity.certificate_sha256 != fingerprint:
            await asyncio.sleep(0.01)


def test_actual_tls_rotation_preserves_cookie_history_identity_and_restart(certificates):
    config, trust, issue, original = certificates

    async def scenario():
        async with mobile_listener(config) as service:
            secret = secrets.token_urlsafe(32)
            invitation = service.devices.create_invitation()
            device = service.devices.claim(invitation["invitation"], "PWA", secret)
            service.devices.approve(device["device_id"], device["verification_code"])
            sessions = SessionStore(config.data_dir / "agent_sessions")
            session = sessions.create()
            sessions.append(session, SessionMessageRecord(role="user", content="Keep the original session"))
            identity_path = config.data_dir / "mobile" / "identity.pem"
            identity_bytes = identity_path.read_bytes()
            history = {p: p.read_bytes() for p in config.data_dir.rglob("*.jsonl")}
            before = await asyncio.to_thread(request, service, trust, secret)
            assert before[0] == 200 and before[2] == original[2]
            assert (await asyncio.to_thread(request, service, trust))[0] == 401
            task, computer_id = service.task, service.identity.computer_id
            renewed = issue()
            Path(config.mobile.tls_certfile).write_bytes(renewed[0])
            # A new certificate with the old key is an expected provider write window.
            await asyncio.sleep(0.1)
            assert await asyncio.to_thread(request, service, trust, secret) == before
            Path(config.mobile.tls_keyfile).write_bytes(renewed[1])
            await until_rotated(service, renewed[2])
            after = await asyncio.to_thread(request, service, trust, secret)
            assert after == (200, before[1], renewed[2])
            assert service.task is task and service.running
            assert service.identity.computer_id == computer_id
            assert local_status(service)["certificate_sha256"] == renewed[2]
            assert local_status(service)["pwa_url"] == ORIGIN + "/mobile/"
            assert identity_path.read_bytes() == identity_bytes
            assert all(p.read_bytes() == data for p, data in history.items())
            assert not list((config.data_dir / "mobile").glob(".tls-*"))
            service.devices.revoke(device["device_id"])
            assert (await asyncio.to_thread(request, service, trust, secret))[0] == 401
        assert not [t for t in asyncio.all_tasks() if t.get_name() == "mobile-tls-reload"]
        async with mobile_listener(config) as restarted:
            assert restarted.identity.computer_id == computer_id
            assert (await asyncio.to_thread(request, restarted, trust, secret))[0] == 401
            assert restarted.identity.certificate_sha256 == renewed[2]

    asyncio.run(scenario())


@pytest.mark.parametrize("invalid", ["malformed", "wrong-domain", "expired", "missing-key"])
def test_invalid_rotation_keeps_last_valid_tls_then_recovers(certificates, invalid):
    config, trust, issue, original = certificates

    async def scenario():
        async with mobile_listener(config) as service:
            certfile, keyfile = Path(config.mobile.tls_certfile), Path(config.mobile.tls_keyfile)
            if invalid == "malformed":
                certfile.write_bytes(b"incomplete certificate write")
            elif invalid == "missing-key":
                keyfile.unlink()
            else:
                rejected = issue(host="other.example" if invalid == "wrong-domain" else "computer.example",
                                 expired=invalid == "expired")
                certfile.write_bytes(rejected[0])
                keyfile.write_bytes(rejected[1])
            await asyncio.sleep(0.12)
            assert (await asyncio.to_thread(request, service, trust))[2] == original[2]
            assert service.running
            renewed = issue()
            certfile.write_bytes(renewed[0])
            keyfile.write_bytes(renewed[1])
            await until_rotated(service, renewed[2])
            assert (await asyncio.to_thread(request, service, trust))[2] == renewed[2]
            # Even if a client still trusts this certificate, its locally expired
            # fallback may not accept another handshake or report readiness.
            service.tls.current = replace(service.tls.current,
                                          expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
            assert not service.running
            with pytest.raises((ssl.SSLError, ConnectionError)):
                await asyncio.to_thread(request, service, trust)

    asyncio.run(scenario())
