import json
from dataclasses import replace

import pytest


def test_connection_api_preserves_config_and_has_recovery_copy(client, tmp_path, monkeypatch):
    import yaml
    from trade_compass_agent.config import invalidate_config_cache

    config = tmp_path / "config.yaml"
    original = {"llm": {"provider": "lmstudio", "model": "existing", "timeout": 42},
                "watchlists": {"stocks": ["600519"]}}
    config.write_text(yaml.safe_dump(original))
    monkeypatch.setenv("TRADE_COMPASS_CONFIG", str(config))
    invalidate_config_cache()
    response = client.patch("/api/agent/connection", json={"provider": "ollama", "model": "gemma4:26b"})
    assert response.status_code == 200
    assert response.json()["provider"] == "ollama"
    assert next(o for o in response.json()["options"] if o["provider"] == "lmstudio")["model"] == "existing"
    assert yaml.safe_load(config.read_text())["llm"]["timeout"] == 42
    assert yaml.safe_load(config.read_text())["watchlists"] == original["watchlists"]
    assert yaml.safe_load(config.with_suffix(".llm-backup.yaml").read_text()) == original
    client.patch("/api/agent/connection", json={"provider": "lmstudio", "model": "existing"})
    assert yaml.safe_load(config.with_suffix(".llm-backup.yaml").read_text()) == original
    assert client.patch("/api/agent/connection", json={"provider": "arbitrary_command"}).status_code == 422
    invalidate_config_cache()


def test_switching_back_restores_custom_connection_without_exposing_credentials(tmp_path, monkeypatch):
    import yaml
    from trade_compass_agent.config import update_llm_connection
    from trade_compass_agent.llm.connection import connection_settings

    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump({"llm": {"provider": "openai", "model": "custom-model",
                                            "api_key_env": "MY_EXISTING_KEY"}}))
    monkeypatch.setenv("TRADE_COMPASS_CONFIG", str(config))
    monkeypatch.setenv("MY_EXISTING_KEY", "secret-not-in-settings")
    local = update_llm_connection("lmstudio", "qwen3.8-27b")
    options = connection_settings(local)
    assert "secret-not-in-settings" not in json.dumps(options)
    previous = next(o for o in options["options"] if o["provider"] == "openai")
    restored = update_llm_connection("openai", previous["model"])
    assert restored.llm.model == "custom-model" and restored.llm.api_key_env == "MY_EXISTING_KEY"




@pytest.mark.parametrize("provider", ["claude_code", "unreleased-provider", "arbitrary_command"])
def test_connections_reject_unknown_providers_without_changing_config(client, tmp_path, monkeypatch, provider):
    from trade_compass_agent.config import invalidate_config_cache, load_app_config, update_llm_connection
    from trade_compass_agent.llm.connection import check_connection
    from trade_compass_agent.llm.providers import create_chat_client, create_llm_provider
    from trade_compass_agent.runtime.exceptions import AgentUnavailableError

    path = tmp_path / "core.yaml"
    original = "llm:\n  provider: lmstudio\n  model: existing\n"
    path.write_text(original)
    monkeypatch.setenv("TRADE_COMPASS_CONFIG", str(path))
    invalidate_config_cache()
    available = client.get("/api/agent/connection").json()
    assert provider not in {option["provider"] for option in available["options"]}
    assert client.patch("/api/agent/connection", json={"provider": provider, "model": ""}).status_code == 422
    with pytest.raises(ValueError, match="Unsupported"):
        update_llm_connection(provider, "")
    assert path.read_text() == original
    config = load_app_config()
    config = replace(config, llm=replace(config.llm, provider=provider))
    assert check_connection(config)["status"] == "unsupported"
    with pytest.raises(AgentUnavailableError, match="Unsupported model connection"):
        create_chat_client(config)
    with pytest.raises(AgentUnavailableError, match="Unsupported model connection"):
        create_llm_provider(provider=provider, enabled=True, config=config)
