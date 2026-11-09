from types import SimpleNamespace

import pytest

from trade_compass_agent.mobile import proxy


@pytest.mark.parametrize("address", ["http://127.0.0.1:7890", "http://[::1]:8080/"])
def test_macos_local_proxy_preserves_explicit_bypass(monkeypatch, address):
    monkeypatch.setattr(proxy, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(proxy, "getproxies", lambda: {
        "https": address, "http": "http://other.example", "all": "socks5://other.example",
        "no": "localhost,127.0.0.1,.example.com",
    })
    assert proxy.local_https_proxy_env() == {
        "HTTPS_PROXY": address, "NO_PROXY": "localhost,127.0.0.1,.example.com",
    }


@pytest.mark.parametrize("address", [
    "", "http://proxy.example:8080", "http://192.168.1.1:8080", "http://127.0.0.1",
    "http://127.0.0.1:0", "http://127.0.0.1:65536", "http://127.0.0.1:bad",
    "http://user:secret@127.0.0.1:8080", "http://@127.0.0.1:8080",
    "socks5://127.0.0.1:8080", "https://127.0.0.1:8080", "http://127.0.0.1:8080/path",
    "http://127.0.0.1:8080?token=secret", "http://127.0.0.1:8080#fragment",
    "http://127.0.0.1:8080\n", "http://[broken",
])
def test_unsupported_or_credentialed_proxy_is_not_forwarded(monkeypatch, address):
    monkeypatch.setattr(proxy, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(proxy, "getproxies", lambda: {"https": address})
    assert proxy.local_https_proxy_env() == {}


def test_linux_keeps_existing_helper_environment(monkeypatch):
    monkeypatch.setattr(proxy, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(proxy, "getproxies", lambda: pytest.fail("unexpected proxy discovery"))
    assert proxy.local_https_proxy_env() == {}
