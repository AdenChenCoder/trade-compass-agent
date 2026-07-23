from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from trade_compass_agent.llm.providers import ChatCompletion, ToolCall


def test_agent_turn_without_api_key_returns_503(client: TestClient) -> None:
    response = client.post(
        "/api/agent/turn",
        json={"message": "今天大盘怎么样"},
    )
    assert response.status_code == 503
    body = response.json()
    detail = body.get("detail", "")
    assert (
        "DEEPSEEK_API_KEY" in detail
        or "OPENAI_API_KEY" in detail
        or "llm.provider" in detail.lower()
        or "LLM" in detail
    )


def test_agent_skills_lists_bundled_skills(client: TestClient) -> None:
    response = client.get("/api/agent/skills")
    assert response.status_code == 200
    names = {item["name"] for item in response.json()["skills"]}
    assert {"intraday-tech", "investment-masters"} <= names


def test_agent_skills_returns_only_external(
    client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Settings API returns only external (project) skills, not self-evolved ones."""
    config_path = tmp_path / "agent_skills.yaml"
    config_path.write_text(
        "enabled_skills:\n  - test-external\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "trade_compass_agent.runtime.skills.AGENT_SKILLS_CONFIG_PATH",
        config_path,
    )

    response = client.get("/api/agent/skills")
    assert response.status_code == 200
    body = response.json()
    sources = {item["source"] for item in body["skills"]}
    assert "memory_vault" not in sources


def _collect_sse_event_names(client: TestClient, session_id: str) -> list[str]:
    names: list[str] = []
    deadline = time.monotonic() + 5.0
    with client.stream("GET", f"/api/agent/stream?session_id={session_id}") as response:
        assert response.status_code == 200
        event_name = "message"
        for raw in response.iter_lines():
            if time.monotonic() > deadline:
                break
            if raw.startswith("event:"):
                event_name = raw.split(":", 1)[1].strip()
                continue
            if raw == "" and event_name:
                names.append(event_name)
                if event_name in {"done", "error", "interrupted"}:
                    break
                event_name = "message"
    return names


def test_load_skill_emits_skill_loaded_sse(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    call_count = {"n": 0}

    class SkillThenReplyClient:
        name = "mock"

        def stream_complete(self, messages, *, tools=None, on_delta=None, is_cancelled=None):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return ChatCompletion(
                    content=None,
                    tool_calls=[
                        ToolCall(
                            id="call_skill",
                            name="load_skill",
                            arguments=json.dumps({"name": "intraday-tech"}),
                        )
                    ],
                    model="mock",
                    provider="mock",
                )
            text = "skill loaded reply"
            if on_delta:
                on_delta(text)
            return ChatCompletion(content=text, model="mock", provider="mock")

    monkeypatch.setattr(
        "trade_compass_agent.runtime.loop.create_chat_client",
        lambda config: SkillThenReplyClient(),
    )

    created = client.post("/api/agent/sessions")
    session_id = created.json()["session_id"]

    collected: list[str] = []

    def consume() -> None:
        collected.extend(_collect_sse_event_names(client, session_id))

    thread = threading.Thread(target=consume, daemon=True)
    thread.start()
    time.sleep(0.05)

    turn = client.post(
        "/api/agent/turn",
        json={"message": "load intraday-tech skill", "session_id": session_id},
    )
    thread.join(timeout=5)

    assert turn.status_code == 200
    assert "skill_loaded" in collected
