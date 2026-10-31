import json
from types import SimpleNamespace

import pytest

from trade_compass_agent.llm.providers import ChatCompletion
from trade_compass_agent.runtime.report_quality import report_issues


@pytest.mark.parametrize("text", ["费用差额45.45。[functions.exec 计算]", "已核验时间。[clock__curr_time]"])
def test_current_tool_citation_needs_a_matching_receipt(text):
    assert any("没有本轮执行回执" in issue for issue in report_issues(text, []))


@pytest.mark.parametrize("text", ["示例引用为[functions.exec]，这里没有执行工具。",
                                 "没有调用[clock__curr_time]，当前时间未知。",
                                 "```text\n[functions.exec]\n```",
                                 "附件差额为45.45。[用户提供的附件]"])
def test_explanations_and_user_attachment_sources_remain_valid(text):
    assert not report_issues(text, [])


def test_real_business_tool_namespace_can_reference_its_receipt():
    assert not report_issues("已读取日线。[functions.get_bars]", [("get_bars", json.dumps({"symbol": "600519"}))])


@pytest.mark.parametrize("corrects", [True, False])
def test_http_corrects_or_rejects_an_invented_tool_citation(client, monkeypatch, corrects):
    wrong = "乙方案174.20，费用差额45.45。[functions.exec 计算]"
    right = "乙方案174.20，费用差额45.45。[用户提供的附件]"
    calls = []

    def respond(*a, **kw):
        calls.append(kw)
        return ChatCompletion(content=wrong if len(calls) == 1 or not corrects else right)

    monkeypatch.setattr("trade_compass_agent.runtime.loop.create_chat_client",
                        lambda _: SimpleNamespace(stream_complete=respond))
    response = client.post("/api/agent/turn", json={"session_id": "citation-case", "message": "根据附件比较费用。",
        "attachments": [{"type": "text", "content": "甲219.65；乙174.20"}]})
    assert len(calls) == 2
    if corrects:
        assert response.status_code == 200 and right in response.json()["summary"]
        assert "functions.exec" not in response.json()["summary"]
    else:
        assert response.status_code == 502
