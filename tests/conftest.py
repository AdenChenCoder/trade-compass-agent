from __future__ import annotations

import os
import socket
from urllib.parse import urlparse
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

_LLM_API_KEY_ENV_VARS = (
    "OPENAI_API_KEY",
    "DEEPSEEK_API_KEY",
    "ANTHROPIC_API_KEY",
    "OPENROUTER_API_KEY",
)
_MISSING = object()
_ORIGINAL_LLM_API_KEYS: dict[str, str | object] = {}


@pytest.fixture(autouse=True)
def _isolate_native_tokenizer(monkeypatch):
    """Unit tests must not depend on the developer's running local model."""
    monkeypatch.setattr("trade_compass_agent.llm.providers._measure_lmstudio_context", lambda *a, **k: None)


@pytest.fixture(autouse=True)
def _isolate_native_user_config(tmp_path, monkeypatch):
    """Native subprocess tests must not use the developer's credentials/config."""
    monkeypatch.setattr("trade_compass_agent.llm.native_runtime.host_codex_home", lambda: tmp_path / "codex-user")


def pytest_sessionstart(session: pytest.Session) -> None:
    """Remove remote credentials; local models are blocked separately below."""
    del session
    for key in _LLM_API_KEY_ENV_VARS:
        _ORIGINAL_LLM_API_KEYS[key] = os.environ.get(key, _MISSING)
        os.environ[key] = ""


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    del session, exitstatus
    for key, value in _ORIGINAL_LLM_API_KEYS.items():
        if value is _MISSING:
            os.environ.pop(key, None)
        else:
            os.environ[key] = str(value)


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("TRADE_COMPASS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("TRADE_COMPASS_MEMORY_DIR", str(tmp_path / "memory"))
    monkeypatch.setenv("TRADE_COMPASS_DATA_PROVIDER", "sample")
    from trade_compass_agent.web.app import app

    return TestClient(app)


@pytest.fixture(autouse=True)
def _isolate_local_model_connections(monkeypatch, request):
    """Local providers need no API key; even swallowed connection attempts fail."""
    from trade_compass_agent.llm.providers import _PROVIDER_BASE_URLS

    ports = {urlparse(_PROVIDER_BASE_URLS[name]).port for name in ("lmstudio", "ollama")}
    attempted = []
    def protect(original):
        def connect(sock, address):
            if (isinstance(address, tuple) and len(address) > 1
                    and address[0] in {"127.0.0.1", "localhost", "::1"} and address[1] in ports):
                attempted.append(address[1])
                raise RuntimeError("Unit tests must explicitly mock local model calls")
            return original(sock, address)
        return connect
    monkeypatch.setattr(socket.socket, "connect", protect(socket.socket.connect))
    monkeypatch.setattr(socket.socket, "connect_ex", protect(socket.socket.connect_ex))
    yield
    if attempted:
        pytest.fail(f"{request.node.nodeid} attempted a live local model connection; inject a model double")
