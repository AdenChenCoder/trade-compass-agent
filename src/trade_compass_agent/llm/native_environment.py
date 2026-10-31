"""Pass configured system proxies to native runtimes without changing the host."""

from __future__ import annotations

import os
import sys
from urllib import request


def native_agent_environment() -> dict[str, str]:
    env = dict(os.environ)
    # An explicit proxy setting, including an empty value, remains authoritative.
    if any(key.lower() in {"http_proxy", "https_proxy", "all_proxy"} for key in env):
        return env
    if sys.platform == "darwin":
        # getproxies() can ignore macOS settings when only NO_PROXY is exported.
        proxies = request.getproxies_macosx_sysconf()
        for scheme in ("http", "https"):
            if proxies.get(scheme):
                env[f"{scheme.upper()}_PROXY"] = proxies[scheme]
        if proxies.get("no") and not any(key.lower() == "no_proxy" for key in env):
            env["NO_PROXY"] = proxies["no"]
    return env
