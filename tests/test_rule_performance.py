from __future__ import annotations

from pathlib import Path

from trade_compass_agent.evaluation.rule_performance import RulePerformanceEvaluator
from trade_compass_agent.ops.audit import JsonAuditLog


def test_rule_performance_counts_source_rules(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    memory_dir = tmp_path / "memory"
    data_dir.mkdir()
    memory_dir.mkdir()

    audit = JsonAuditLog(data_dir / "audit.jsonl")
    audit.record(
        event_type="recommendation",
        summary="600519 test",
        payload={
            "symbol": "600519",
            "source_rules": ["ma_trend", "volume_ratio"],
            "grade_out": "dip_candidate",
        },
    )
    audit.record(
        event_type="recommendation",
        summary="510300 test",
        payload={
            "symbol": "510300",
            "source_rules": ["ma_trend"],
            "grade_out": "observe",
        },
    )

    report = RulePerformanceEvaluator(data_dir=data_dir, memory_dir=memory_dir).evaluate(limit=50)
    by_id = {row.rule_id: row for row in report.rows}
    assert by_id["ma_trend"].signal_count == 2
    assert by_id["volume_ratio"].signal_count == 1


def test_rule_performance_api(client) -> None:
    response = client.get("/api/evaluation/rules")
    assert response.status_code == 200
    body = response.json()
    assert "rows" in body
    assert isinstance(body["rows"], list)
