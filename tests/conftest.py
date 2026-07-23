from __future__ import annotations

import os
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


def pytest_sessionstart(session: pytest.Session) -> None:
    """Keep the test process offline unless a test injects an explicit fake key."""
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
