from concurrent.futures import ThreadPoolExecutor
import hashlib
import ipaddress
import json
from pathlib import Path
import socket
import ssl
import subprocess
import sys

from cryptography import x509
from cryptography.hazmat.primitives import serialization
import pytest
import yaml

from trade_compass_agent.config import MobileConfig
from trade_compass_agent.mobile.identity import ComputerIdentity
from trade_compass_agent.mobile.setup import create_test_kit
from trade_compass_agent.mobile.tls import browser_tls


def test_kit_uses_real_tls_trust_matching_ip_and_private_files(tmp_path):
    directory = tmp_path / 'kit'
    result = create_test_kit('192.168.1.17', directory)
    root = x509.load_der_x509_certificate((directory / 'public-ca.cer').read_bytes())
    leaf = x509.load_pem_x509_certificate((directory / 'server.pem').read_bytes())
    assert leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(
        x509.IPAddress) == [ipaddress.ip_address('192.168.1.17')]
    assert root.extensions.get_extension_for_class(x509.BasicConstraints).value.path_length == 0
    fragment = yaml.safe_load((directory / 'mobile-fragment.yaml').read_text())
    assert list(fragment) == ['mobile']
    assert 'enabled' not in fragment['mobile']
    config = MobileConfig(**fragment['mobile'])
    identity = ComputerIdentity('existing-computer', 'unused', Path('unused'))
    loaded, _ = browser_tls(config, identity)
    assert loaded.computer_id == identity.computer_id
    assert result['mobile_url'] == 'https://192.168.1.17:19705/mobile/'
    assert set(p.name for p in directory.iterdir()) == {
        'public-ca.cer', 'server.pem', 'server-key.pem', 'README.md', 'mobile-fragment.yaml'}
    if sys.platform != 'win32':
        assert directory.stat().st_mode & 0o777 == 0o700
        assert all(p.stat().st_mode & 0o777 == 0o600 for p in directory.iterdir())
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(config.tls_certfile, config.tls_keyfile)
    client_context = ssl.create_default_context(cadata=root.public_bytes(serialization.Encoding.PEM).decode())
    # Connect locally while verifying the actual LAN identity, with certificate checks enabled.
    with socket.socket() as listener, ThreadPoolExecutor(max_workers=1) as pool:
        listener.bind(('127.0.0.1', 0))
        listener.listen()
        listener.settimeout(5)

        def serve():
            raw, _ = listener.accept()
            raw.settimeout(5)
            with raw:
                try:
                    with server_context.wrap_socket(raw, server_side=True) as connection:
                        connection.sendall(b'computer')
                except ssl.SSLError:
                    pass

        for hostname, trusted in [('192.168.1.17', True), ('192.168.1.18', False)]:
            future = pool.submit(serve)
            with socket.create_connection(listener.getsockname(), timeout=5) as raw:
                if trusted:
                    with client_context.wrap_socket(raw, server_hostname=hostname) as connection:
                        assert connection.recv(8) == b'computer'
                else:
                    with pytest.raises(ssl.SSLCertVerificationError):
                        client_context.wrap_socket(raw, server_hostname=hostname)
            future.result(timeout=5)
    before = {p.name: hashlib.sha256(p.read_bytes()).digest() for p in directory.iterdir()}
    with pytest.raises(FileExistsError):
        create_test_kit('192.168.1.18', directory)
    assert before == {p.name: hashlib.sha256(p.read_bytes()).digest() for p in directory.iterdir()}


@pytest.mark.parametrize('address', ['127.0.0.1', '0.0.0.0', '8.8.8.8', '224.0.0.1', '::1', 'fe80::1%en0', 'localhost'])
def test_invalid_address_does_not_write(address, tmp_path):
    with pytest.raises(ValueError):
        create_test_kit(address, tmp_path / 'kit')
    assert not (tmp_path / 'kit').exists()


def test_packaged_module_command_leaves_current_config_untouched(tmp_path, monkeypatch):
    config = tmp_path / 'existing.yaml'
    config.write_text('mobile:\n  enabled: false\ndata_dir: original-data\n')
    monkeypatch.setenv('TRADE_COMPASS_CONFIG', str(config))
    before = config.read_bytes()
    result = subprocess.run([sys.executable, '-m', 'trade_compass_agent.mobile.setup',
        '--address', '192.168.10.25', '--output', str(tmp_path / 'kit')],
        capture_output=True, text=True, check=True, cwd=tmp_path)
    assert json.loads(result.stdout)['mobile_url'] == 'https://192.168.10.25:19705/mobile/'
    assert config.read_bytes() == before
    assert not (tmp_path / 'original-data').exists()
