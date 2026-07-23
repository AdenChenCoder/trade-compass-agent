from pathlib import Path

from trade_compass_agent.ops.audit import JsonAuditLog


def test_json_audit_log_get_and_recommendations(tmp_path: Path):
    audit = JsonAuditLog(tmp_path / "audit.jsonl")
    event = audit.record(
        "recommendation",
        "600519 observe",
        {"symbol": "600519", "grade_out": "observe", "evidence": ["x"]},
    )
    reloaded = JsonAuditLog(tmp_path / "audit.jsonl")
    assert reloaded.get(event.id) is not None
    assert reloaded.recommendations(10)[0].id == event.id
