from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from trade_compass_agent.config import AgentConfig, AppConfig, LLMConfig
from trade_compass_agent.llm.providers import ChatCompletion, ToolCall
from trade_compass_agent.runtime import loop
from trade_compass_agent.runtime.report_quality import assess_data_quality
from trade_compass_agent.runtime.tools.memory import tool_search_memory, tool_write_memory


@pytest.mark.parametrize("same_note", [True, False])
def test_corrected_archive_scope_is_reported_from_real_write_receipts(tmp_path, monkeypatch, same_note):
    original = "紫鹭合成笔记：样例材料不用于投资判断。"
    saved = original if same_note else "另一条合成笔记：每份资料独立标注来源。"
    completions = [
        ChatCompletion(content=None, tool_calls=[ToolCall("bad", "write_memory", json.dumps({"scope": "验收", "content": original}))]),
        ChatCompletion(content=None, tool_calls=[ToolCall("good", "write_memory", json.dumps({"scope": "acceptance", "content": saved}))]),
        ChatCompletion(content="已保存合成归档。"),
    ]
    monkeypatch.setattr(loop, "create_chat_client", lambda *_: SimpleNamespace(
        stream_complete=lambda *a, **k: completions.pop(0)))
    monkeypatch.setattr(loop, "shared_consolidation_worker", lambda **_: None)
    config = AppConfig(data_dir=tmp_path / "data", memory_dir=tmp_path / "memory", data_provider="sample",
        llm=LLMConfig(provider="disabled"), agent=AgentConfig(learning_enabled=False, llm_session_titles=False))
    agent = loop.AgentLoop.from_config(config)
    response = agent.run_turn("保存合成归档，分类参数有误时修正后继续。", session_id="archive-recovery")
    assert "暂不可用" not in response.summary
    if same_note:
        assert not response.warnings
        assert "数据可用性" not in response.summary
    else:
        assert any("归档记忆未保存（验收）" in warning for warning in response.warnings)
        assert "归档记忆未保存（验收）" in response.summary
    assert not response.degraded
    rows = [json.loads(line) for line in (config.data_dir / "agent_sessions/archive-recovery.jsonl").read_text().splitlines()]
    receipts = [json.loads(row["content"]) for row in rows if row.get("role") == "tool"]
    assert receipts[0]["error_type"] == "invalid_scope"
    assert (receipts[0]["content_hash"] == receipts[1]["content_hash"]) is same_note
    assert rows[-1]["content"] == response.summary
    hits = json.loads(tool_search_memory(config.memory_dir, saved))["results"]
    assert hits and saved in hits[0]["content"]
    assert len(list((config.memory_dir / "tree").rglob("*.md"))) == 1


@pytest.mark.parametrize("failure", ["runtime", "unconfirmed", "fact_check", "legacy_invalid_scope"])
def test_successful_archive_does_not_hide_other_failed_outcomes(tmp_path, failure):
    saved = json.loads(tool_write_memory(tmp_path, "acceptance", "这是需要保留完整失败记录的合成内容。"))
    error = {"error": "earlier failure", "scope": "acceptance", "content_hash": saved["content_hash"]}
    if failure == "unconfirmed":
        error.update(error_type="invalid_scope", execution_unconfirmed=True)
    elif failure == "fact_check":
        error.update(error_type="invalid_scope", fact_check_failed=True, issues=["原始事实未核验"])
    elif failure == "legacy_invalid_scope":
        error.update(error_type="invalid_scope")
        error.pop("content_hash")
    quality = assess_data_quality([("write_memory", json.dumps(error)), ("write_memory", json.dumps(saved))])
    assert quality.warnings
    assert quality.degraded is (failure == "fact_check")


def test_archive_recovery_keeps_unrelated_market_query_gaps(tmp_path):
    content = "合成研究归档，不改变行情可用性。"
    failed = tool_write_memory(tmp_path, "验收", content)
    saved = tool_write_memory(tmp_path, "acceptance", content)
    quality = assess_data_quality([
        ("get_bars", json.dumps({"symbol": "600519", "timeframe": "1d", "error": "missing"})),
        ("get_bars", json.dumps({"symbol": "600519", "timeframe": "5m", "bars": [{}]})),
        ("get_bars", json.dumps({"symbol": "000858", "timeframe": "1d", "bars": [{}]})),
        ("write_memory", failed), ("write_memory", saved),
    ])
    assert quality.degraded
    assert len(quality.warnings) == 1 and "600519" in quality.warnings[0]
