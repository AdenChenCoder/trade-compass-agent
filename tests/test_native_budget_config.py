from __future__ import annotations

from dataclasses import asdict

import pytest
import yaml

from trade_compass_agent.config import LLMConfig, invalidate_config_cache, load_app_config


@pytest.fixture
def config_path(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    monkeypatch.setenv("TRADE_COMPASS_CONFIG", str(path))
    monkeypatch.setenv("TRADE_COMPASS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("TRADE_COMPASS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("TRADE_COMPASS_MEMORY_DIR", str(tmp_path / "memory"))
    invalidate_config_cache()
    yield path
    invalidate_config_cache()


def test_existing_configuration_gets_independent_native_budget_default(config_path):
    config_path.write_text(yaml.safe_dump({"llm": {"provider": "codex"}, "agent": {"max_tool_rounds": 7}}))
    config = load_app_config()
    assert config.llm.native_max_tool_calls == LLMConfig().native_max_tool_calls == 30
    assert config.agent.max_tool_rounds == 7


@pytest.mark.parametrize("limit", [1, 30, 47])
def test_native_tool_budget_survives_yaml_round_trip(config_path, limit):
    expected = LLMConfig(provider="codex", model="", native_max_tool_calls=limit)
    config_path.write_text(yaml.safe_dump({"llm": asdict(expected)}))
    assert load_app_config().llm == expected


@pytest.mark.parametrize("invalid", [0, -1, True, 1.5, "5", None])
@pytest.mark.parametrize("source", ["dataclass", "yaml"])
def test_native_tool_budget_requires_positive_integer(config_path, invalid, source):
    with pytest.raises(ValueError, match="native_max_tool_calls must be an integer >= 1"):
        if source == "dataclass":
            LLMConfig(native_max_tool_calls=invalid)
        else:
            config_path.write_text(yaml.safe_dump({"llm": {"native_max_tool_calls": invalid}}))
            load_app_config()


@pytest.mark.parametrize("initial,other", [("codex", "lmstudio"), ("lmstudio", "codex")])
def test_connection_api_preserves_native_budget_across_provider_switch(client, config_path, monkeypatch, initial, other):
    monkeypatch.setattr("trade_compass_agent.ops.tick_scheduler.reload_active_scheduler", lambda: None)
    original = {
        "llm": {"provider": initial, "model": "custom", "native_max_tool_calls": 9,
                "agent_timeout": 120, "timeout": 20},
        "agent": {"max_tool_rounds": 4},
    }
    config_path.write_text(yaml.safe_dump(original))

    for provider in (other, initial):
        response = client.patch("/api/agent/connection", json={"provider": provider, "model": "custom"})
        assert response.status_code == 200
        assert response.json()["provider"] == provider
        persisted = yaml.safe_load(config_path.read_text())
        assert persisted["llm"]["native_max_tool_calls"] == 9
        reloaded = load_app_config()
        assert reloaded.llm.native_max_tool_calls == 9
        assert reloaded.llm.agent_timeout == 120
        assert reloaded.llm.timeout == 20
        assert reloaded.agent.max_tool_rounds == 4

    assert yaml.safe_load(config_path.with_suffix(".llm-backup.yaml").read_text()) == original
