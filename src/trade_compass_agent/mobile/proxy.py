"""Pass an existing macOS local HTTPS proxy to the isolated provider process."""
from ipaddress import ip_address
import os
import sys
from urllib import request
from urllib.parse import urlsplit
from urllib.request import getproxies


def local_https_proxy_env() -> dict[str, str]:
    if sys.platform != "darwin":
        return {}
    # getproxies includes macOS System Configuration, which the Go helper does
    # not read. Keep its credential isolation: only an unauthenticated local
    # HTTP CONNECT proxy is admitted, never arbitrary inherited environment.
    # Runtime imports add NO_PROXY for market data. urllib then returns only
    # those exclusions instead of macOS settings. Explicit proxy overrides,
    # including empty values, still take precedence over system discovery.
    if any(
        key.lower() in {"http_proxy", "https_proxy", "all_proxy"} for key in os.environ
    ):
        proxies = request.getproxies_environment()
    else:
        proxies = getproxies()
        proxies = {**request.getproxies_macosx_sysconf(), **proxies}
    raw = proxies.get("https", "")
    try:
        proxy = urlsplit(raw)
        if (not raw or any(ord(char) < 33 for char in raw)
                or proxy.scheme != "http" or not ip_address(proxy.hostname).is_loopback
                or not proxy.port or proxy.username is not None or proxy.password is not None
                or proxy.path not in ("", "/") or proxy.query or proxy.fragment):
            return {}
    except (TypeError, ValueError):
        return {}
    result = {"HTTPS_PROXY": raw}
    if proxies.get("no"):
        result["NO_PROXY"] = proxies["no"]
    return result
