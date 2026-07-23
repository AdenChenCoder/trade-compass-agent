from __future__ import annotations

import json

from trade_compass_agent.ops.run_content import (
    extract_analysis_from_artifact,
    extract_analysis_from_step_data,
    extract_analysis_from_workflow_output,
    workflow_run_message,
)


def test_extract_analysis_from_workflow_primary_output() -> None:
    analysis = "# 盘前操作建议\n\n" + "这是完整结果。" * 20
    output = {
        "message": "Agent 分析完成",
        "data": {"analysis": analysis},
        "steps": [
            {"output": json.dumps({"data": {"analysis": "较短"}})},
        ],
    }

    assert extract_analysis_from_workflow_output(output) == analysis
    assert workflow_run_message("premarket_briefing", output, analysis) == "premarket_briefing: 盘前操作建议"


def test_extract_analysis_from_step_data_and_artifact(tmp_path) -> None:
    analysis = "# 今日复盘\n\n" + "正文内容。" * 20
    raw = json.dumps({"workflow_id": "eod_review", "analysis": analysis}, ensure_ascii=False)
    assert extract_analysis_from_step_data(raw) == analysis

    artifact = tmp_path / "workflow.jsonl"
    artifact.write_text(
        json.dumps({"run_id": "old", "data": {"analysis": "忽略" * 20}}, ensure_ascii=False)
        + "\n"
        + json.dumps({"run_id": "target", "data": {"analysis": analysis}}, ensure_ascii=False)
        + "\n",
        encoding="utf-8",
    )

    assert extract_analysis_from_artifact(str(artifact), run_id="target") == analysis


def test_extract_analysis_does_not_use_intermediate_step_when_primary_failed() -> None:
    sector_analysis = "# A股板块资金流向分析\n\n" + "这只是中间步骤。" * 20
    output = {
        "workflow_id": "morning_plan",
        "primary_step_id": "agent_plan",
        "steps": [
            {
                "step_id": "sector_flow",
                "output": json.dumps({"data": {"analysis": sector_analysis}}, ensure_ascii=False),
            },
            {
                "step_id": "agent_plan",
                "output": json.dumps({"error": "scheduler-agent-morning_plan timed out"}, ensure_ascii=False),
            },
        ],
    }

    assert extract_analysis_from_workflow_output(output) is None
    assert (
        workflow_run_message("morning_plan", {**output, "degraded": True, "error": "agent timeout"}, None)
        == "morning_plan: agent_plan failed/degraded - agent timeout"
    )
