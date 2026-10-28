import json
from types import SimpleNamespace

from trade_compass_agent.ops.run_content import extract_analysis_from_workflow_output, workflow_run_message
from trade_compass_agent.runtime.workflows.engine import WorkflowStepResult, _compose_workflow_output


def test_secondary_degradation_keeps_actual_step_and_reason_in_parent_result():
    manifest = SimpleNamespace(id="postmarket_archive", version=1, risk_policy={},
        steps=[SimpleNamespace(id="dreaming", primary_output=True)])
    outputs = {
        "knowledge_curator": WorkflowStepResult("knowledge_curator", "tool", "curate", json.dumps({"data": {
            "degraded": True, "warnings": ["知识冲突审查未完成，需后续重试。"]}})),
        "dreaming": WorkflowStepResult("dreaming", "tool", "dreaming", {"data": {"patterns": 0}}),
    }
    result = _compose_workflow_output(manifest, {}, outputs, [])
    assert result["degraded"] is True
    assert result["degraded_steps"] == ["knowledge_curator"]
    assert "知识冲突审查未完成" in result["warnings"][0]
    message = workflow_run_message(manifest.id, result, None)
    assert "knowledge_curator" in message and "dreaming degraded" not in message


def test_secondary_failure_does_not_hide_a_valid_primary_report():
    manifest = SimpleNamespace(id="review", version=1, risk_policy={},
        steps=[SimpleNamespace(id="report", primary_output=True)])
    outputs = {
        "curator": WorkflowStepResult("curator", "tool", "curate", {"degraded": True, "warnings": ["review pending"]}),
        "report": WorkflowStepResult("report", "tool", "report", {"analysis": "已完成的有效报告，仍应在任务详情中显示。"}),
    }
    result = _compose_workflow_output(manifest, {}, outputs, [])
    assert result["degraded"] is True and result["degraded_steps"] == ["curator"]
    assert extract_analysis_from_workflow_output(result) == outputs["report"].output["analysis"]


def test_partial_primary_report_remains_readable_but_explicitly_failed_output_is_hidden():
    manifest = SimpleNamespace(id="review", version=1, risk_policy={},
        steps=[SimpleNamespace(id="report", primary_output=True)])
    for failed in [False, True]:
        primary = {"analysis": "已有部分结果和明确的数据缺口。", "degraded": True, "warnings": ["价格缺失"]}
        if failed:
            primary["error"] = "报告核验失败"
        result = _compose_workflow_output(manifest, {}, {"report": WorkflowStepResult("report", "tool", "report", primary)}, [])
        assert extract_analysis_from_workflow_output(result) == (None if failed else primary["analysis"])
