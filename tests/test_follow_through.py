from datetime import datetime, timedelta

from trade_compass_agent.data import SampleProvider
from trade_compass_agent.domain import AuditEvent
from trade_compass_agent.evaluation import FollowThroughEvaluator


def test_follow_through_evaluator_scores_audit_events():
    event = AuditEvent(
        id="audit-1",
        timestamp=datetime.now() - timedelta(days=20),
        event_type="recommendation",
        summary="600519 observe",
        payload={"symbol": "600519", "grade_out": "observe"},
    )
    report = FollowThroughEvaluator(SampleProvider()).evaluate([event])
    assert len(report.results) == 1
    assert report.results[0].symbol == "600519"
    assert report.metrics
    assert any(metric.name == "sample_count" for metric in report.metrics)
