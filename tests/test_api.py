from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from trade_compass_agent.ops.audit import JsonAuditLog


def _get(client: TestClient, path: str) -> dict | list:
    response = client.get(path)
    assert response.status_code == 200, f"GET {path} -> {response.status_code}: {response.text[:200]}"
    return response.json()


def test_market_pulse(client: TestClient) -> None:
    body = _get(client, "/api/market-pulse")
    assert {"timestamp", "provider_name", "sectors", "limit_up", "notes"} <= body.keys()
    assert isinstance(body["sectors"], list)


def test_bars(client: TestClient) -> None:
    body = _get(client, "/api/bars?symbol=600519&timeframe=1d&limit=30")
    assert body["symbol"] == "600519"
    assert body["timeframe"] == "1d"
    assert body["limit"] == 30
    assert isinstance(body["bars"], list) and body["bars"]
    assert {"symbol", "timestamp", "open", "high", "low", "close", "volume"} <= body["bars"][0].keys()


def test_events(client: TestClient) -> None:
    body = _get(client, "/api/events?symbol=600519&limit=3")
    assert body["symbol"] == "600519"
    assert isinstance(body["events"], list)


def test_workflow_api_prefers_v2_assets(client: TestClient) -> None:
    workflows = _get(client, "/api/workflows")
    by_id = {item["id"]: item for item in workflows}

    assert by_id["morning_plan"]["asset_version"] == 2
    assert by_id["morning_plan"]["steps"]

    detail = _get(client, "/api/workflows/morning_plan")
    assert detail["asset_version"] == 2
    step_types = [step["type"] for step in detail["steps"]]
    assert "tool" in step_types
    assert "workflow" in step_types

    validation = _get(client, "/api/workflows/morning_plan/validation")
    assert validation["ok"] is True
    assert any(item["name"] == "steps" and item["ok"] for item in validation["checks"])
    assert any(item["name"] == "output_schema_schema" and item["ok"] for item in validation["checks"])


def test_portfolio_get_empty(client: TestClient) -> None:
    body = _get(client, "/api/portfolio")
    assert {"accounts", "positions_by_account", "trades", "realized_trades", "costs"} <= body.keys()
    assert body["trades"] == []


def test_portfolio_trade_valid_and_invalid(client: TestClient) -> None:
    valid_payload = {
        "symbol": "600519",
        "account": "short_stock",
        "side": "buy",
        "quantity": 100,
        "price": 10.0,
        "reason": "api test",
    }
    response = client.post("/api/portfolio/trades", json=valid_payload)
    assert response.status_code == 200, response.text
    body = response.json()
    assert {"accounts", "trades"} <= body.keys()
    assert len(body["trades"]) == 1
    assert body["trades"][0]["symbol"] == "600519"
    assert body["trades"][0]["account"] == "short_stock"
    assert body["trades"][0]["trade_id"]
    assert body["trades"][0]["price_source"] == "user_confirmed"

    invalid_payload = dict(valid_payload, quantity=37)
    invalid_response = client.post("/api/portfolio/trades", json=invalid_payload)
    # Lot size no longer enforced at OMS — odd quantities are accepted
    assert invalid_response.status_code == 200


def test_audit_listing_and_lookup(client: TestClient, tmp_path) -> None:
    audit_path = tmp_path / "data" / "audit.jsonl"
    audit = JsonAuditLog(audit_path)
    event = audit.record(
        "recommendation",
        "600519 observe",
        payload={"symbol": "600519"},
    )
    listing = _get(client, "/api/audit?limit=10")
    assert isinstance(listing, list)
    assert any(item["id"] == event.id for item in listing)

    detail = _get(client, f"/api/audit/{event.id}")
    assert detail["id"] == event.id
    assert detail["event_type"] == "recommendation"

    missing = client.get("/api/audit/does-not-exist")
    assert missing.status_code == 404


def test_rules_crud(client: TestClient) -> None:
    listing = _get(client, "/api/rules")
    assert {"content", "entries", "version", "chars_used", "limit"} <= listing.keys()
    assert listing["entries"] == []

    create_response = client.post("/api/rules/entries", json={"text": "API smoke rule"})
    assert create_response.status_code == 200, create_response.text
    entries = create_response.json()["entries"]
    assert len(entries) == 1
    entry_id = entries[0]["id"]
    assert create_response.headers["x-rules-version"]

    update_response = client.patch(f"/api/rules/entries/{entry_id}", json={"text": "updated smoke rule"})
    assert update_response.status_code == 200, update_response.text
    assert update_response.json()["entries"][0]["id"] == entry_id
    assert update_response.json()["entries"][0]["text"] == "updated smoke rule"

    replace_response = client.put("/api/rules", json={"content": "§\nfull replacement"})
    assert replace_response.status_code == 200, replace_response.text
    assert replace_response.json()["entries"][0]["text"] == "full replacement"

    delete_id = replace_response.json()["entries"][0]["id"]
    delete_response = client.delete(f"/api/rules/entries/{delete_id}")
    assert delete_response.status_code == 200, delete_response.text
    assert delete_response.json()["entries"] == []

    assert client.post("/api/rules/propose", json={}).status_code in {404, 405}
    assert client.post("/api/rules/approve", json={}).status_code in {404, 405}


def test_memory_pin_and_forget_api(client: TestClient) -> None:
    text = "用户确认固定的交易纪律"

    pin_response = client.post("/api/memory/memory/pin", json={"content": text})
    assert pin_response.status_code == 200, pin_response.text
    pinned = pin_response.json()["entries"][0]
    assert pinned["text"] == text
    assert pinned["source"] == "user_pin"
    assert pinned["confidence"] == 1.0
    assert pinned["status"] == "active"

    forget_response = client.post("/api/memory/memory/forget", json={"content": "用户确认固定"})
    assert forget_response.status_code == 200, forget_response.text
    forgotten = forget_response.json()["entries"][0]
    assert forgotten["text"] == text
    assert forgotten["status"] == "archived"
    assert forgotten["confidence"] == 0.0


def test_jobs_listing_and_run(client: TestClient) -> None:
    jobs = _get(client, "/api/jobs")
    assert isinstance(jobs, list)
    job_ids = {job["id"] for job in jobs}
    assert job_ids == {"premarket", "morning_plan", "close", "eod_review", "postmarket", "weekly"}
    for job in jobs:
        assert "delivery_channels" in job
        assert isinstance(job["delivery_channels"], list)

    premarket_detail = _get(client, "/api/jobs/premarket")
    assert premarket_detail["workflow_id"] == "premarket_briefing"
    assert premarket_detail["steps"] == []
    postmarket_detail = _get(client, "/api/jobs/postmarket")
    assert postmarket_detail["workflow_id"] == "postmarket_archive"
    assert postmarket_detail["steps"] == []

    patched = client.patch(
        "/api/jobs/eod_review",
        json={"delivery_channels": ["web_log", "feishu"]},
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["delivery_channels"] == ["web_log", "feishu"]

    jobs_after = _get(client, "/api/jobs")
    eod = next(j for j in jobs_after if j["id"] == "eod_review")
    assert eod["delivery_channels"] == ["web_log", "feishu"]

    run_response = client.post("/api/jobs/postmarket/run")
    assert run_response.status_code == 200, run_response.text
    run_body = run_response.json()
    assert run_body["job_id"] == "postmarket"
    # Job may fail in test env (no LLM available for Agent steps); verify it ran
    assert run_body["id"]

    runs = _get(client, "/api/jobs/runs?limit=5")
    assert isinstance(runs, list) and runs
    assert runs[0]["job_id"] == "postmarket"

    missing = client.post("/api/jobs/unknown-job/run")
    assert missing.status_code == 404


def test_custom_jobs_crud_and_route_order(client: TestClient) -> None:
    created = client.post(
        "/api/jobs/custom",
        json={
            "name": "测试任务",
            "prompt": "输出 hello",
            "schedule": "trading_day 10:00",
            "delivery_channels": ["web_log", "feishu"],
        },
    )
    assert created.status_code == 200, created.text
    body = created.json()
    job_id = body["id"]
    assert body["delivery_channels"] == ["web_log", "feishu"]

    listed = _get(client, "/api/jobs/custom")
    assert any(item["id"] == job_id for item in listed)

    patched = client.patch(
        f"/api/jobs/custom/{job_id}",
        json={"delivery_channels": ["web_log", "weixin"]},
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["delivery_channels"] == ["web_log", "weixin"]

    deleted = client.delete(f"/api/jobs/custom/{job_id}")
    assert deleted.status_code == 200


def test_notifications(client: TestClient) -> None:
    import time
    client.post("/api/jobs/close/run")
    body: list = []
    for _ in range(10):
        body = _get(client, "/api/notifications?limit=5")
        if body:
            break
        time.sleep(0.5)
    assert isinstance(body, list)
    assert body, "expected at least one notification after running a job"
    assert {"channel", "title", "message", "severity"} <= body[0].keys()


def test_config_watchlists(client: TestClient) -> None:
    body = _get(client, "/api/config/watchlists")
    assert {"stocks", "etfs", "mid_term"} <= body.keys()
    assert body["stocks"], "watchlist should not be empty under default config"


def test_scheduler_config_get_and_patch(client: TestClient, tmp_path, monkeypatch) -> None:
    config_path = tmp_path / "scheduler-test.yaml"
    config_path.write_text(
        "scheduler:\n  enabled: true\n  premarket_time: \"08:50\"\n  close_time: \"15:10\"\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("TRADE_COMPASS_CONFIG", str(config_path))
    from trade_compass_agent.config import invalidate_config_cache

    invalidate_config_cache()

    body = _get(client, "/api/config/scheduler")
    assert body["enabled"] is True
    assert body["premarket_time"] == "08:50"

    patch = client.patch("/api/config/scheduler", json={"premarket_time": "08:55"})
    assert patch.status_code == 200, patch.text
    patched = patch.json()
    assert patched["config"]["premarket_time"] == "08:55"
    assert "message" in patched

    invalid = client.patch("/api/config/scheduler", json={"premarket_time": "invalid"})
    assert invalid.status_code == 422


def test_decisions_curate_and_reflect(client: TestClient, tmp_path: Path) -> None:
    from trade_compass_agent.memory.decision_store import DecisionStore

    data_dir = tmp_path / "data"
    store = DecisionStore(data_dir)
    d = store.store_decision(symbol="600549", side="buy", quantity=100, price=90.0, account="short_stock", reasoning="板块强势")
    store.resolve(symbol="600549", account="short_stock", sell_price=80.0)

    listed = _get(client, "/api/decisions")
    assert listed["stats"]["awaiting_reflection"] == 1
    assert listed["decisions"][-1]["status"] == "resolved"

    curated = client.post("/api/decisions/curate", json={"max_reflect": 5})
    assert curated.status_code == 200, curated.text
    body = curated.json()
    assert body["reflected_count"] == 1
    assert body["reflected_ids"] == [d.id]
    assert body["stats"]["reflected"] == 1

    manual = client.post(f"/api/decisions/{d.id}/reflect", json={"reflection": "手动复盘"})
    assert manual.status_code == 409

    d2 = store.store_decision(symbol="002938", side="buy", quantity=100, price=20.0, account="a")
    store.resolve(symbol="002938", account="a", sell_price=19.0)
    reflected = client.post(f"/api/decisions/{d2.id}/reflect")
    assert reflected.status_code == 200, reflected.text
    assert reflected.json()["decision"]["status"] == "reflected"
    assert reflected.json()["decision"]["reflection"]
