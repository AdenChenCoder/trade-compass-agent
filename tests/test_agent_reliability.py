"""Consumer regressions for the October 19–20 unattended runs."""
import asyncio
import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from trade_compass_agent.config import AgentConfig, AppConfig, load_app_config
from trade_compass_agent.domain import AccountKind, PaperTrade
from trade_compass_agent.ops.delivery import DeliveryRouter
from trade_compass_agent.ops.delivery_store import DeliveryStore, LEASE_SECONDS
from trade_compass_agent.ops.job_definition import DeliveryConfig
from trade_compass_agent.ops.run_store import SqliteRunStore
from trade_compass_agent.portfolio.simulator import PaperPortfolio
from trade_compass_agent.runtime.report_quality import assess_data_quality, report_issues
from trade_compass_agent.runtime.verifier import unverified_execution_claim


def encoded(name, **data):
    return name, json.dumps(data, ensure_ascii=False)


FACTS = [encoded("sina_realtime_quote", quotes=[{"symbol": "601928", "name": "凤凰传媒", "price": 8.92}]),
         encoded("compute_ma", symbol="601928", ma={"MA5": 9.072, "MA20": 8.557})]


def test_closed_positions_never_participate_in_quote_refresh(monkeypatch):
    portfolio = PaperPortfolio()
    for symbol, side, day in [("000001", "buy", 2), ("000001", "sell", 1), ("601928", "buy", 1)]:
        portfolio.record(PaperTrade(symbol=symbol, account=AccountKind.SHORT_STOCK, side=side,
            quantity=100, price=10, timestamp=datetime.now() - timedelta(days=day), reason="test"))
    seen = []
    monkeypatch.setattr(portfolio, "_try_sina_batch_quote",
        lambda symbols: (seen.append(symbols) or ({"601928": 9}, {"601928": "凤凰传媒"})))
    assert portfolio.refresh_market_prices(None) == {"601928": 9}
    assert seen == [{"601928"}]
    assert portfolio.positions_with_market_prices()[0].name == "凤凰传媒"
    assert len(portfolio.trades) == 3


@pytest.mark.parametrize("text", ["601928（新华传媒）持有", "601928现价8.92元高于MA5", "601928 开盘9.14 < 9.12"])
def test_observed_identity_and_arithmetic_errors_are_rejected(text):
    assert report_issues(text, FACTS)


def test_correct_report_forecasts_and_score_rows_do_not_false_alarm():
    assert not report_issues("601928（凤凰传媒）现价低于MA5，高于MA20\n| RSI | 0 中性 |\n如果价格站上MA5再评估。", FACTS)
    other = [encoded("sina_realtime_quote", quotes=[{"symbol": "000001", "name": "平安银行", "price": 20}])]
    assert not report_issues("601928（凤凰传媒）现价低于MA5\n000001（平安银行）继续观察", FACTS + other)
    assert not report_issues("现价尚未站上MA5，若站上MA5再观察", FACTS)
    assert report_issues("开盘价9.14 < 9.12", [])


def test_key_failure_and_optional_fallback_have_different_outcomes():
    failed = assess_data_quality([encoded("analyze_portfolio", error="timeout"),
                                  encoded("get_market_pulse", warnings=["已使用备用行业分类"] )])
    assert failed.degraded and failed.warnings
    recovered = assess_data_quality([encoded("get_bars", symbol="601928", error="timeout"),
        encoded("get_bars", symbol="601928", bars=[{"close": 9}]),
        encoded("get_fund_flow", unavailable=["concept"])])
    assert not recovered.degraded and len(recovered.warnings) == 1


def test_execution_quantity_and_price_annotation_is_not_a_company_name():
    assert not report_issues("已卖出601928（100 股 @9.00），本轮成交回执已核对。", FACTS)
    assert report_issues("601928（新华传媒）当前持仓100股", FACTS)
    assert report_issues("601928（新华传媒 100股）", FACTS)


def test_stale_portfolio_is_degraded_and_mixed_negation_cannot_hide_fill():
    assert assess_data_quality([encoded("analyze_portfolio", positions=[
        {"symbol": "601928", "price_is_fresh": False}])]).degraded
    assert unverified_execution_claim("本轮未成交；已成交100股，trade_id=fake", [])
    receipt = [encoded("place_paper_trade", status="executed", trade_id="actual", symbol="601928", side="buy", quantity=100)]
    assert unverified_execution_claim("本轮已卖出601928共200股", receipt)
    assert not unverified_execution_claim("本轮已买入601928共100股，trade_id=actual", receipt)


@pytest.mark.parametrize("suffix,unverified", [
    ("本轮已卖出000001共100股，trade_id=actual", False),
    ("本轮已卖出000001共100股 trade_id=fabricated", True),
    ("本轮已卖出000001共200股 trade_id=actual", True),
    ("本轮已买入000001共100股 trade_id=actual", True),
])
def test_retracted_quoted_history_does_not_mask_current_execution_checks(suffix, unverified):
    receipt = [encoded("place_paper_trade", status="executed", trade_id="actual", symbol="000001", side="sell", quantity=100)]
    correction = "此前记忆中「10/19第2轮已卖出000001（trade_id=old-fake）」与当时账本不符；"
    assert unverified_execution_claim(correction + suffix, receipt) is unverified
    assert unverified_execution_claim("此前记忆中「已卖出000001（trade_id=old-fake）」", receipt)


@pytest.mark.parametrize("label", ["已成交（本轮）：无", "本轮已成交：0笔", "已成交：无", "| 已成交（本轮） | 无 |",
                                 "已成交：无（本轮零下单）", "已成交：无（本轮零成交）", "已成交：0笔（本次无交易）"])
def test_explicit_empty_execution_labels_are_not_fill_claims(label):
    assert not unverified_execution_claim(label, [])
    assert unverified_execution_claim(label + "；本轮已卖出000001共100股 trade_id=fake", [])
    assert unverified_execution_claim("已成交：无（本轮已卖出000001共100股 trade_id=fake）", [])


def test_consumer_publishes_matching_fill_and_retracted_history_without_replaying_order(tmp_path, monkeypatch):
    from trade_compass_agent.llm.providers import ChatCompletion, ToolCall
    from trade_compass_agent.runtime import loop
    from trade_compass_agent.runtime.tools.registry import ToolRegistry
    config = AppConfig(data_dir=tmp_path / "data", memory_dir=tmp_path / "memory", data_provider="sample",
                       agent=AgentConfig(learning_enabled=False, llm_session_titles=False))
    draft = "此前记忆中「已卖出000001（trade_id=old-fake）」与当时账本不符。\n本轮已卖出000001共100股 trade_id=actual。"
    completions = [ChatCompletion(content="", tool_calls=[ToolCall(id="sell", name="place_paper_trade", arguments="{}")]),
                   ChatCompletion(content=draft)]
    calls = []
    def execute(self, name, arguments):
        calls.append(name)
        return encoded(name, status="executed", trade_id="actual", symbol="000001", side="sell", quantity=100)[1]
    monkeypatch.setattr(loop, "create_chat_client", lambda *_: SimpleNamespace(stream_complete=lambda *args, **kwargs: completions.pop(0)))
    monkeypatch.setattr(loop, "shared_consolidation_worker", lambda **_: None)
    monkeypatch.setattr(ToolRegistry, "execute", execute)
    result = loop.AgentLoop.from_config(config).run_turn("核对本轮成交与旧记忆的矛盾", session_id="scheduler-execution-quote-test")
    assert draft in result.summary and calls == ["place_paper_trade"]
    assert not (config.data_dir / "paper_trades.jsonl").exists()


def test_report_repair_has_no_tools_and_never_publishes_rejected_draft(tmp_path, monkeypatch):
    from trade_compass_agent.llm.providers import ChatCompletion, ToolCall
    from trade_compass_agent.runtime import loop
    from trade_compass_agent.runtime.tools.registry import ToolRegistry
    config = AppConfig(data_dir=tmp_path / "data", memory_dir=tmp_path / "memory", data_provider="sample",
        agent=AgentConfig(learning_enabled=False, llm_session_titles=False))
    requests = []
    completions = [ChatCompletion(content="", tool_calls=[ToolCall(id=str(i), name=name, arguments="{}") for i, (name, _) in enumerate(FACTS)]),
        ChatCompletion(content="601928（新华传媒）现价高于MA5。"),
        ChatCompletion(content="601928（凤凰传媒）现价低于MA5，高于MA20，本轮未成交。")]
    def stream(*args, on_delta=None, **kwargs):
        requests.append(kwargs)
        result = completions.pop(0)
        if on_delta and result.content:
            on_delta(result.content)
        return result
    monkeypatch.setattr(loop, "create_chat_client", lambda *_: SimpleNamespace(stream_complete=stream))
    monkeypatch.setattr(loop, "shared_consolidation_worker", lambda **_: None)
    monkeypatch.setattr(ToolRegistry, "execute", lambda self, name, args: dict(FACTS)[name])
    events = []
    agent = loop.AgentLoop.from_config(config, on_event=events.append)
    result = agent.run_turn("分析当前持仓", session_id="scheduler-quality-test")
    assert "凤凰传媒" in result.summary and "新华传媒" not in result.summary
    assert requests[-1]["tools"] is None
    assert not any("新华传媒" in e.data.get("text", "") for e in events if e.event == "delta")
    diagnostic = next((config.data_dir / "report_diagnostics").glob("*.json"))
    assert "新华传媒" in json.loads(diagnostic.read_text())["draft"]
    assert diagnostic.stat().st_mode & 0o777 == 0o600
    assert not (config.data_dir / "paper_trades.jsonl").exists()


def test_delivery_retries_after_restart_without_rerunning_job(client, monkeypatch):
    config = load_app_config()
    store = SqliteRunStore(config.data_dir / "scheduler.db")
    run = store.create_run("premarket")
    store.complete_run(run, message="原始分析结果")
    sent = []
    class Adapter:
        def delivery_unavailable_reason(self, msg):
            return None
        def send_sync(self, msg):
            sent.append(msg)
            return len(sent) > 1
    router = SimpleNamespace(get_adapter=lambda name: Adapter())
    monkeypatch.setattr("trade_compass_agent.ops.delivery._build_channel_router", lambda: router)
    DeliveryRouter(config).deliver(run, DeliveryConfig(channels=("feishu",)))
    detail = client.get(f"/api/jobs/runs/{run.id}").json()
    assert detail["deliveries"][0]["status"] == "retry"
    restart = DeliveryRouter(config)
    restart.retry_due()
    assert len(sent) == 1  # Persisted backoff survives reconstruction.
    with restart._deliveries._conn() as conn:
        conn.execute("UPDATE job_deliveries SET next_attempt=0")
    restart.retry_due()
    restart.deliver(run, DeliveryConfig(channels=("feishu",)))
    assert len(sent) == 2 and sent[0].metadata == sent[1].metadata
    assert sent[0].content == sent[1].content == "原始分析结果"
    detail = client.get(f"/api/jobs/runs/{run.id}").json()
    assert detail["status"] == "completed" and detail["message"] == "原始分析结果"
    assert detail["deliveries"][0]["status"] == "completed"
    assert len(detail["step_runs"]) == 1 and detail["step_runs"][0]["status"] == "completed"


def test_delivery_claim_is_exclusive_and_crash_recovery_is_bounded(tmp_path):
    path = tmp_path / "scheduler.db"
    store = DeliveryStore(path)
    identity = store.enqueue("run", "feishu", {})
    first = store.claim(identity, now=1)
    restarted = DeliveryStore(path)
    assert restarted.claim(identity, now=2) is None
    second = restarted.claim(identity, now=1 + LEASE_SECONDS)
    assert second["attempts"] == 2
    store.finish(first, sent=True, now=123)  # Expired worker cannot overwrite the new lease.
    assert store.for_run("run")[0]["status"] == "sending"
    third = store.claim(identity, now=1 + LEASE_SECONDS * 2)
    assert third["attempts"] == 3
    assert store.claim(identity, now=1 + LEASE_SECONDS * 3) is None
    assert store.for_run("run")[0]["status"] == "failed"


def test_critical_data_gaps_reach_job_detail_with_analysis_preserved(client, monkeypatch):
    from datetime import date
    from trade_compass_agent.ops.agent_session import run_agent_step
    from trade_compass_agent.ops.job_definition import JobDefinition, StepContext
    from trade_compass_agent.ops.job_executor import JobExecutor
    from trade_compass_agent.runtime import loop
    from trade_compass_agent.runtime.types import TurnResponse
    from trade_compass_agent.runtime.workflows.engine import WorkflowStepResult, _compose_workflow_output
    config = load_app_config()
    fake = SimpleNamespace(_tools=SimpleNamespace(), run_turn=lambda *a, **kw:
        TurnResponse(session_id="test", summary="价格缺失，未作交易判断", warnings=["持仓价格未更新"], degraded=True))
    monkeypatch.setattr(loop.AgentLoop, "from_config", lambda *a, **kw: fake)
    def workflow(*args, **kwargs):
        result = asyncio.run(run_agent_step(StepContext(config=config, date=date.today()), "检查持仓", "premarket"))
        manifest = SimpleNamespace(id="probe", version=1, risk_policy={},
            steps=[SimpleNamespace(id="primary", primary_output=True)])
        output = WorkflowStepResult(step_id="primary", type="tool", uses="probe",
            output=json.dumps({"data": result.data}), warnings=tuple(result.data["warnings"]))
        return _compose_workflow_output(manifest, {}, {"primary": output}, list(output.warnings))
    monkeypatch.setattr("trade_compass_agent.runtime.workflows.engine.run_workflow_asset_by_id", workflow)
    store = SqliteRunStore(config.data_dir / "scheduler.db")
    job = JobDefinition(id="premarket", name="test", description="", schedule="", workflow_id="probe")
    run = asyncio.run(JobExecutor(config, store).execute(job, trigger="api"))
    detail = client.get(f"/api/jobs/runs/{run.id}").json()
    assert detail["status"] == "degraded" and detail["ok"] is False
    assert detail["analysis"] == "价格缺失，未作交易判断"
    assert detail["step_runs"][0]["data"]["warnings"] == ["持仓价格未更新"]


@pytest.mark.parametrize("recover", [True, False])
def test_review_format_repair_keeps_exact_versions_and_preserves_candidate(tmp_path, recover):
    from trade_compass_agent.memory.memory_store import MemoryStore
    from trade_compass_agent.memory.reassessment import review_candidates
    store = MemoryStore(tmp_path)
    created = store.add("交易前核验行情时效", source="scheduler")
    calls = []
    def model(system, user):
        payload = json.loads(user)
        calls.append(payload)
        row = payload["candidates"][0]
        return json.dumps({"reviews": [{"entry_id": row["entry_id"],
            "version": row["version"] if recover and "validation_error" in payload else "wrong-version",
            "decision": "defer", "reason": "暂缺进一步依据", "refs": []}]})
    result = review_candidates(store, model)
    assert result["ok"] is recover and len(calls) == 2
    row = next(m for m in MemoryStore(tmp_path).get_entries_with_meta() if m.entry_id == created["entry_id"])
    assert row.status == "candidate" and not store.list_active()
    if not recover:
        assert "version mismatch" in result["error"]
        assert not store.maintenance_marker("candidate_reviewed_fingerprint")


def test_news_search_fallback_is_free_dated_and_explicitly_scoped(monkeypatch):
    from trade_compass_agent.runtime.tools import search
    monkeypatch.setenv("WEB_SEARCH_PROVIDER", "free")
    monkeypatch.setattr(search, "_web_search_ddg", lambda **kw: json.dumps({"error": "unavailable"}))
    feed = b'<rss><channel><item><title>Market report</title><link>https://example.com/news</link><pubDate>Tue, 20 Oct 2026 16:00:00 GMT</pubDate><source>Publisher</source></item></channel></rss>'
    monkeypatch.setattr("requests.get", lambda *a, **kw: SimpleNamespace(content=feed, raise_for_status=lambda: None))
    result = json.loads(search.tool_web_search(query="market", limit=1))
    assert result["provider"] == "google_news" and result["data_scope"] == "news"
    assert result["data_status"] == "fallback" and result["warnings"]
    assert result["results"][0]["published_date"].startswith("2026-10-20T16:00")


def test_identity_reconciliation_keeps_original_trade_history_and_backup(tmp_path):
    from trade_compass_agent.memory.instrument_store import InstrumentStore
    store = InstrumentStore(tmp_path / "memory")
    store.append_trade("601928", "buy", 500, 9.21, reason="601928新华传媒建仓")
    original = store.recall("601928")
    result = store.reconcile_name("601928", "凤凰传媒", recovery_dir=tmp_path / "recovery")
    page = store.recall("601928")
    assert page.startswith("# 601928 - 凤凰传媒")
    assert "601928新华传媒建仓" in page and "证券身份核对：601928=凤凰传媒" in page
    from pathlib import Path
    assert Path(result["backup"]).read_text() == original
    assert not store.reconcile_name("601928", "凤凰传媒", recovery_dir=tmp_path / "recovery")["changed"]


def test_feishu_retry_keeps_original_recipients_and_request_identity(tmp_path, monkeypatch):
    pytest.importorskip("lark_oapi")
    from trade_compass_agent.channels.base import ChannelMessage
    from trade_compass_agent.channels.feishu import FeishuBotAdapter
    adapter = FeishuBotAdapter(app_id="test", app_secret="fake", subscribers_path=tmp_path / "subscribers.json")
    adapter._subscriber_chats = {"original"}
    requests = []
    def create(request):
        requests.append(request.request_body)
        return SimpleNamespace(success=lambda: True)
    client = SimpleNamespace(im=SimpleNamespace(v1=SimpleNamespace(message=SimpleNamespace(create=create))))
    monkeypatch.setattr(adapter, "_get_lark_client", lambda: client)
    msg = ChannelMessage(content="原报告", metadata={"delivery_id": "stable", "delivery_targets": adapter.delivery_targets()})
    assert adapter.send_sync(msg)
    adapter._subscriber_chats.add("new")
    assert adapter.send_sync(msg)
    assert [r.receive_id for r in requests] == ["original", "original"]
    assert requests[0].uuid == requests[1].uuid
    adapter._subscriber_chats = {"new"}
    assert not adapter.send_sync(msg) and len(requests) == 2


def test_local_health_check_does_not_use_system_proxy(monkeypatch):
    from unittest.mock import MagicMock
    from trade_compass_agent.daemon.status import probe_health
    opener = MagicMock()
    opener.open.return_value.__enter__.return_value.status = 200
    build = MagicMock(return_value=opener)
    monkeypatch.setattr("urllib.request.build_opener", build)
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **kw: pytest.fail("system proxy path used"))
    assert probe_health("127.0.0.1", 19704) == (True, "ok")
    assert build.call_args.args[0].proxies == {}
