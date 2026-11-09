from types import SimpleNamespace
import os
from urllib import request

import pytest

from trade_compass_agent.mobile import proxy


@pytest.fixture(autouse=True)
def isolated_proxy_settings(monkeypatch):
    for key in list(os.environ):
        if key.lower().endswith("_proxy"):
            monkeypatch.delenv(key)
    monkeypatch.setattr(request, "getproxies_macosx_sysconf", lambda: {}, raising=False)


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


def test_market_bypass_does_not_hide_macos_proxy(monkeypatch):
    from trade_compass_agent.data.network import extend_no_proxy_for_eastmoney

    monkeypatch.setattr(proxy, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setenv("NO_PROXY", "localhost")
    monkeypatch.setenv("no_proxy", "localhost")
    extend_no_proxy_for_eastmoney()
    assert request.getproxies() == {"no": os.environ["NO_PROXY"]}
    monkeypatch.setattr(request, "getproxies_macosx_sysconf",
                        lambda: {"https": "http://127.0.0.1:7890"}, raising=False)
    assert proxy.local_https_proxy_env() == {
        "HTTPS_PROXY": "http://127.0.0.1:7890", "NO_PROXY": os.environ["NO_PROXY"],
    }


@pytest.mark.parametrize("key,value", [
    ("HTTPS_PROXY", ""), ("https_proxy", ""), ("HTTP_PROXY", "http://proxy.example:8080"),
    ("ALL_PROXY", "socks5://proxy.example:8080"), ("https_proxy", "http://secret@127.0.0.1:7890"),
])
def test_explicit_proxy_override_prevents_system_fallback(monkeypatch, key, value):
    monkeypatch.setattr(proxy, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setenv(key, value)
    monkeypatch.setenv("NO_PROXY", "localhost")
    monkeypatch.setattr(request, "getproxies_macosx_sysconf",
                        lambda: pytest.fail("explicit proxy override must remain authoritative"), raising=False)
    assert proxy.local_https_proxy_env() == {}
