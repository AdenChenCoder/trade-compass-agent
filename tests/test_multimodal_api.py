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


def test_turn_accepts_attachment_schema(client: TestClient, monkeypatch) -> None:
    from trade_compass_agent.runtime.types import TurnResponse

    received = {}

    def run_turn(self, message, *, session_id, attachments, **kwargs):
        received.update(message=message, attachments=attachments)
        return TurnResponse(session_id=session_id, summary="已读取附件")

    monkeypatch.setattr("trade_compass_agent.web.agent_api.AgentLoop.run_turn", run_turn)
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
    assert response.status_code == 200, response.text
    assert response.json()["summary"] == "已读取附件"
    assert received == {
        "message": "分析",
        "attachments": [
            {"type": "text", "content": "附加上下文"},
            {"type": "url", "url": "https://example.com"},
        ],
    }


def test_agent_mcp_endpoint(client: TestClient) -> None:
    response = client.get("/api/agent/mcp")
    assert response.status_code == 200
    body = response.json()
    assert "servers" in body
