import importlib.util
import json
import socket
import ssl
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID


spec = importlib.util.spec_from_file_location(
    "mobile_ingress_diagnostics", Path(__file__).parents[1] / "scripts/mobile_ingress_diagnostics.py"
)
probe_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe_module)


@pytest.mark.parametrize("address", ["127.0.0.1", "198.18.0.1", "10.0.0.1", "::1"])
def test_private_probe_target_rejected(address):
    with pytest.raises(ValueError):
        probe_module.target_from(json.dumps({
            "origin": "https://mobile.example.ts.net", "ips": [address], "computer_id": "fixture"
        }))


@pytest.mark.parametrize("reachable,exit_code", [(True, 0), (False, 1)])
def test_remote_run_preserves_failure_status_and_target_privacy(monkeypatch, capsys, reachable, exit_code):
    monkeypatch.setenv("COMPASS_TLS_PROBE_TARGET", json.dumps({
        "origin": "https://private.example.ts.net", "ips": ["1.1.1.1"], "computer_id": "private-id"
    }))
    monkeypatch.setattr(probe_module.time, "sleep", lambda _: None)
    monkeypatch.setattr(probe_module.time, "monotonic", lambda: 0)
    monkeypatch.setattr(probe_module, "probe", lambda *args: {"ok": reachable, "stage": "complete"})
    assert probe_module.main() == exit_code
    output = capsys.readouterr().out
    assert len(output.splitlines()) == 6
    assert all(value not in output for value in ("private.example", "private-id", "1.1.1.1"))


def test_network_failure_does_not_print_provider_exception(monkeypatch):
    def failed_connect(*args, **kwargs):
        raise OSError("private-target-and-provider-details")

    monkeypatch.setattr(probe_module.socket, "create_connection", failed_connect)
    result = probe_module.probe("private.example.ts.net", "1.1.1.1", "private-id")
    assert not result["ok"] and result["stage"] == "tcp"
    assert "private" not in json.dumps(result)


def test_real_https_identity_and_certificate_verification(tmp_path, monkeypatch, capsys):
    host = "mobile.example.ts.net"
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host)])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(host)]), critical=False)
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .sign(key, hashes.SHA256()))
    certfile, keyfile = tmp_path / "cert.pem", tmp_path / "key.pem"
    certfile.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    keyfile.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                         serialization.NoEncryption()))
    keyfile.chmod(0o600)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            assert self.path == "/mobile/v1/browser/connection"
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"computer_id":"fixture"}')

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certfile, keyfile)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connect, default_context = socket.create_connection, ssl.create_default_context
    monkeypatch.setattr(probe_module.socket, "create_connection",
                        lambda address, timeout: connect(server.server_address, timeout=timeout))
    monkeypatch.setattr(probe_module.ssl, "create_default_context",
                        lambda: default_context(cafile=str(certfile)))
    try:
        assert probe_module.probe(host, "1.1.1.1", "fixture")["ok"]
        wrong_identity = probe_module.probe(host, "1.1.1.1", "another")
        assert not wrong_identity["ok"] and wrong_identity["stage"] == "identity"
        wrong_host = probe_module.probe("another.example.ts.net", "1.1.1.1", "fixture")
        assert not wrong_host["ok"] and wrong_host["stage"] == "tls"
        assert capsys.readouterr().out == ""
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
