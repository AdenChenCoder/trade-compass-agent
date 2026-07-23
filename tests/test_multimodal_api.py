from __future__ import annotations

from fastapi.testclient import TestClient


def test_turn_rejects_invalid_attachment_type(client: TestClient) -> None:
    response = client.post(
        "/api/agent/turn",
        json={
            "message": "hello",
            "attachments": [{"type": "video", "url": "https://x.com"}],
        },
    )
    assert response.status_code == 422


def test_turn_accepts_attachment_schema(client: TestClient) -> None:
    response = client.post(
        "/api/agent/turn",
        json={
            "message": "分析",
            "attachments": [
                {"type": "text", "content": "附加上下文"},
                {"type": "url", "url": "https://example.com"},
            ],
        },
    )
    # LLM unavailable in tests → 503, but schema validation passed
    assert response.status_code in {503, 200}


def test_agent_mcp_endpoint(client: TestClient) -> None:
    response = client.get("/api/agent/mcp")
    assert response.status_code == 200
    body = response.json()
    assert "servers" in body
