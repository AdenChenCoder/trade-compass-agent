"""End-to-end consumer regressions for the closed-loop recovery fixes."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta
import json
from pathlib import Path
from types import SimpleNamespace

from trade_compass_agent.config import AgentConfig, AppConfig, LLMConfig
from trade_compass_agent.data import SampleProvider
from trade_compass_agent.data.providers import LocalBarCacheProvider
from trade_compass_agent.domain import AccountKind, Bar, PaperTrade
from trade_compass_agent.evaluation.follow_through import FollowThroughEvaluator
from trade_compass_agent.evaluation.rule_performance import RulePerformanceEvaluator
from trade_compass_agent.evaluation.signal_migration import reconcile_historical_signals
from trade_compass_agent.evaluation.signal_tracker import SignalTracker
from trade_compass_agent.llm.providers import ChatCompletion, ToolCall
from trade_compass_agent.memory.capture_hooks import should_capture
from trade_compass_agent.memory.memory_store import MemoryStore
from trade_compass_agent.memory.observation_store import ObservationStore
from trade_compass_agent.memory.promotion import apply_promotions, rank_promotion_candidates
from trade_compass_agent.memory.skill_store import SkillStore
from trade_compass_agent.memory.write_gate import SemanticWriteGate
from trade_compass_agent.ops.audit import JsonAuditLog
from trade_compass_agent.ops.job_definition import StepContext
from trade_compass_agent.ops.reflection import PendingReflection
from trade_compass_agent.ops.reflection_resolver import resolve_pending_with_market
from trade_compass_agent.portfolio import JsonPaperPortfolio
from trade_compass_agent.portfolio.accounts import AccountStore
from trade_compass_agent.portfolio.trading_policy import AutonomousTradingStore
from trade_compass_agent.runtime import loop
from trade_compass_agent.runtime.report_quality import assess_data_quality, report_issues
from trade_compass_agent.runtime.tools import builtin_operations, portfolio as portfolio_tools
from trade_compass_agent.runtime.tools.portfolio import revoke_and_reconcile_paper_trade, tool_place_paper_trade
from trade_compass_agent.runtime.tools.registry import ToolRegistry
from trade_compass_agent.runtime.tools.signals import tool_emit_signal


def test_trading_signal_follow_through_rule_attribution_and_summaries(tmp_path: Path):
    config = AppConfig(data_dir=tmp_path / "data", memory_dir=tmp_path / "memory", data_provider="sample")
    stack = SimpleNamespace(config=config)
    provider = SampleProvider()
    bars = provider.get_bars("600519", timeframe="1d", limit=180)
    signal_day = bars[-10].timestamp

    # Emit buy, strong_sell, and hold signals via the real tool
    buy_res = json.loads(tool_emit_signal(
        stack,
        symbol="600519",
        rating="buy",
        confidence=0.8,
        reasoning="趋势向上",
        source_skills=["buy-sell-decision-framework"],
    ))
    sell_res = json.loads(tool_emit_signal(
        stack,
        symbol="600519",
        rating="strong_sell",
        confidence=0.85,
        reasoning="破位走弱",
        source_skills=["risk-veto"],
    ))
    hold_res = json.loads(tool_emit_signal(
        stack,
        symbol="600519",
        rating="hold",
        confidence=0.6,
        reasoning="继续观察",
        source_skills=["key-level-tracking"],
    ))
    assert buy_res["status"] == sell_res["status"] == hold_res["status"] == "recorded"

    # Rewrite audit timestamps into historical bar window so 5-day follow-through completes
    audit_path = config.data_dir / "audit.jsonl"
    rewritten = []
    for line in audit_path.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        row["timestamp"] = signal_day.isoformat()
        rewritten.append(json.dumps(row, ensure_ascii=False))
    audit_path.write_text("\n".join(rewritten) + "\n", encoding="utf-8")

    audit = JsonAuditLog(audit_path)
    ft_report = FollowThroughEvaluator(provider).evaluate(audit.events, limit=50)
    assert len(ft_report.results) == 3
    by_action = {r.action: r for r in ft_report.results}
    assert by_action["buy"].return_1d == -by_action["strong_sell"].return_1d
    metrics = {m.name: m.value for m in ft_report.metrics}
    assert metrics["sample_count"] == 3
    assert metrics["directional_count"] == 2
    assert metrics["hold_count"] == 1
    assert metrics["return_1d_count"] == 2  # Hold excluded from directional hit rate

    rule_report = RulePerformanceEvaluator(
        data_dir=config.data_dir, memory_dir=config.memory_dir, provider=provider
    ).evaluate(limit=50)
    rows = {r.rule_id: r for r in rule_report.rows}
    assert rows["buy-sell-decision-framework"].signal_count == 1
    assert rows["buy-sell-decision-framework"].layer == "skill"
    assert rows["buy-sell-decision-framework"].win_rate_1d is not None
    assert rows["key-level-tracking"].signal_count == 1
    assert rows["key-level-tracking"].win_rate_1d is None  # Hold does not distort win rate

    # Verify default RulePerformanceEvaluator (no injected provider) reads historical/short LocalBarCacheProvider bars
    cache = LocalBarCacheProvider(config.data_dir / "market_cache")
    cache.write_bars("600519", "1d", bars[-30:-2], requested_limit=28)  # Stale & <180 bars, yet covers signal_day+5d
    default_rule_report = RulePerformanceEvaluator(
        data_dir=config.data_dir, memory_dir=config.memory_dir
    ).evaluate(limit=50)
    assert not default_rule_report.warnings
    default_rows = {r.rule_id: r for r in default_rule_report.rows}
    assert default_rows["buy-sell-decision-framework"].avg_return_1d == rows["buy-sell-decision-framework"].avg_return_1d

    ctx = StepContext(config=config, date=signal_day.date(), job_id="postmarket", run_id="r1")
    daily_out = asyncio.run(builtin_operations.write_audit_summary(ctx))
    daily_md = Path(daily_out.data["artifact"]).read_text(encoding="utf-8")
    assert "(none)" not in daily_md and "600519" in daily_md

    weekly_out = asyncio.run(builtin_operations.write_weekly_summary(ctx))
    weekly_md = Path(weekly_out.data["artifact"]).read_text(encoding="utf-8")
    assert "(none)" not in weekly_md and "600519" in weekly_md

    # Verify non-entry signals are tracked as 'observed', not 'pending'
    tracked = {r.signal_id: r for r in SignalTracker(config.data_dir)._load_all()}
    assert tracked[buy_res["signal_id"]].status == "pending"
    assert tracked[sell_res["signal_id"]].status == "observed"
    assert tracked[hold_res["signal_id"]].status == "observed"


def test_autonomous_buy_auto_creates_signal_and_closes_on_sell_and_expires_stale(tmp_path: Path, monkeypatch):
    now = datetime(2026, 11, 26, 13, 36, 40)
    config = AppConfig(
        data_dir=tmp_path / "data",
        memory_dir=tmp_path / "memory",
        data_provider="sample",
        agent=AgentConfig(llm_session_titles=False),
    )
    monkeypatch.setattr(portfolio_tools, "_market_now", lambda: now)
    monkeypatch.setattr("trade_compass_agent.ops.trading_calendar.is_trading_day", lambda day: True)
    AccountStore(config.data_dir / "accounts.json").update("short_stock", capital=100_000)
    AutonomousTradingStore(config.data_dir).set_enabled(True)

    stack = SimpleNamespace(
        config=config,
        provider=SimpleNamespace(get_bars=lambda *a, **k: [
            Bar(symbol="002042", timestamp=now, open=3.99, high=4.02, low=3.74, close=3.99, volume=50000)
        ]),
    )
    registry = ToolRegistry(stack, memory_actor="scheduler")
    registry.execute("load_skill", {"name": "autonomous-paper-trading"})

    # 1) Missing 'side' argument returns deterministic trade_rejected + invalid_arguments
    bad = json.loads(registry.execute("place_paper_trade", {
        "symbol": "002042", "quantity": 1000, "account": "short_stock", "reason": "漏传side测试",
    }))
    assert bad["trade_rejected"] is True
    assert bad["error_type"] == "invalid_arguments"
    assert bad["execution_started"] is False

    # 2) Subsequent valid autonomous buy auto-creates and links a TradingSignal
    good = json.loads(registry.execute("place_paper_trade", {
        "symbol": "002042", "side": "buy", "quantity": 1000, "account": "short_stock", "reason": "回踩确认买入",
    }))
    assert good["status"] == "executed"
    assert good["signal_id"]

    # Check report quality on the combined tool log (exact 11/26 13:36 scenario)
    tool_log = [("place_paper_trade", json.dumps(bad)), ("place_paper_trade", json.dumps(good))]
    quality = assess_data_quality(tool_log, auxiliary_memory_scopes=frozenset({"trade-log", "market-notes"}))
    assert not quality.degraded
    assert not any("暂不可用" in w for w in quality.warnings)
    assert bad["error"] in quality.execution_warnings

    tracker = SignalTracker(config.data_dir)
    active = tracker.get_active()
    assert len(active) == 1 and active[0].signal_id == good["signal_id"]
    assert "autonomous-paper-trading" in active[0].source_skills

    # 3) Next-day sell closes the tracked signal with FIFO net PnL
    next_day = now + timedelta(days=1)
    monkeypatch.setattr(portfolio_tools, "_market_now", lambda: next_day)
    stack.provider.get_bars = lambda *a, **k: [
        Bar(symbol="002042", timestamp=next_day, open=4.30, high=4.35, low=4.20, close=4.30, volume=60000)
    ]
    sold = json.loads(registry.execute("place_paper_trade", {
        "symbol": "002042", "side": "sell", "quantity": 1000, "account": "short_stock", "reason": "止盈卖出",
    }))
    assert sold["status"] == "executed"
    closed = tracker.get_closed(outcome_basis="execution_net_pnl")
    assert len(closed) == 1 and closed[0].signal_id == good["signal_id"]
    assert closed[0].outcome == "win"

    # 4) Stale unexecuted buy signal expires cleanly
    stale = json.loads(tool_emit_signal(
        stack, symbol="600519", rating="buy", confidence=0.75, reasoning="候选未触发",
    ))
    records = tracker._load_all()
    for r in records:
        if r.signal_id == stale["signal_id"]:
            r.emitted_at = (now - timedelta(days=10)).isoformat()
    tracker._save_all(records)
    assert tracker.expire_pending(as_of=now.date(), max_trading_days=5) == 1
    reloaded = {r.signal_id: r for r in tracker._load_all()}
    assert reloaded[stale["signal_id"]].status == "expired"


def test_interrupted_paper_trade_reconciles_from_ledger_and_blocks_late_commit(tmp_path: Path):
    config = AppConfig(data_dir=tmp_path / "data", memory_dir=tmp_path / "memory", data_provider="sample")
    AccountStore(config.data_dir / "accounts.json").update("short_stock", capital=50_000)
    stack = SimpleNamespace(config=config, provider=SampleProvider())

    # Case A: Trade already committed before timeout -> recovered as 'executed'
    committed_raw = tool_place_paper_trade(
        stack,
        symbol="000001",
        side="buy",
        quantity=100,
        price=10.0,
        price_source="user_confirmed",
        account="short_stock",
        trade_id="committed-order-01",
    )
    assert json.loads(committed_raw)["status"] == "executed"
    reconciled = revoke_and_reconcile_paper_trade(config.data_dir, config.trading_costs, "committed-order-01")
    assert reconciled is not None
    assert json.loads(reconciled)["trade_id"] == "committed-order-01"

    # Case B: Trade had NOT committed when revoked -> returns None and blocks any late commit
    assert revoke_and_reconcile_paper_trade(config.data_dir, config.trading_costs, "late-order-02") is None
    late_attempt = json.loads(tool_place_paper_trade(
        stack,
        symbol="000001",
        side="buy",
        quantity=100,
        price=10.0,
        price_source="user_confirmed",
        account="short_stock",
        trade_id="late-order-02",
    ))
    assert late_attempt["trade_rejected"] is True
    assert late_attempt["code"] == "execution_timed_out_before_commit"


def test_scheduled_memory_verification_catches_real_conflicts_without_degrading_turn(tmp_path: Path, monkeypatch):
    pulse = json.dumps({
        "timestamp": "2026-11-26T10:05:33",
        "_meta": {"retrieved_at": "2026-11-26T10:05:33+08:00"},
        "indices": [{"name": "上证指数", "price": 3260.0, "date": "2026-11-26", "time": "10:05:30"}],
        "sectors": [{"name": "电子信息", "change_pct": 5.7}],
        "limit_up": {"count": 60, "strong_count": 12, "top_industries": ["电子信息"]},
    }, ensure_ascii=False)
    ma = json.dumps({
        "symbol": "002042",
        "ma": {"MA5": 3.89, "MA10": 3.789, "MA20": 3.542, "MA60": 3.656},
    }, ensure_ascii=False)
    receipts = [("get_market_pulse", pulse), ("compute_ma", ma)]

    # Interactive report_issues has zero false positives on valid multi-clause Chinese comparisons / subsets
    assert report_issues("成本3.80元、现价4.20元高于压力位4.00元。", receipts, reference_date="2026-11-26") == []
    assert report_issues("002042短期均线呈多头排列（MA5>MA10>MA20），计划止损3.60元、目标4.60元。", receipts, reference_date="2026-11-26") == []

    # Scheduled write_memory enforces unverified execution guard + review_report_evidence,
    # keeping built-in autonomous_trading non-degraded on auxiliary trade-log rejection
    # while custom prompt jobs still degrade on unrecovered fact_check_failed.
    completions = [
        ChatCompletion(content=None, tool_calls=[
            ToolCall("p", "get_market_pulse", "{}"),
            ToolCall("m", "write_memory", json.dumps({"scope": "trade-log", "content": "本轮已买入002042共2000股。"})),
        ]),
        ChatCompletion(content="本轮盘中观察完成，暂不交易。"),
    ]
    monkeypatch.setattr(loop, "create_chat_client", lambda *_: SimpleNamespace(
        stream_complete=lambda *a, **k: completions.pop(0)))
    monkeypatch.setattr(loop, "shared_consolidation_worker", lambda **_: None)
    config = AppConfig(
        data_dir=tmp_path / "data",
        memory_dir=tmp_path / "memory",
        data_provider="sample",
        llm=LLMConfig(provider="disabled"),
        agent=AgentConfig(learning_enabled=False, llm_session_titles=False),
    )
    agent = loop.AgentLoop.from_config(config, memory_actor="scheduler", report_date="2026-11-26")
    monkeypatch.setattr(agent._tools, "execute", lambda name, args: pulse if name == "get_market_pulse" else tool_emit_signal(SimpleNamespace(config=config)))
    resp = agent.run_turn("执行盘中检查", session_id="scheduler-autonomous_trading-test")
    assert not resp.degraded
    assert any("记忆未保存（trade-log）" in w for w in resp.warnings)
    assert not list((config.memory_dir / "tree").rglob("*.md"))

    # Custom prompt job writing memory still degrades if memory validation fails
    prompt_completions = [
        ChatCompletion(content=None, tool_calls=[
            ToolCall("m2", "write_memory", json.dumps({"scope": "trade-log", "content": "本轮已买入002042共2000股。"})),
        ]),
        ChatCompletion(content="归档尝试结束。"),
    ]
    monkeypatch.setattr(loop, "create_chat_client", lambda *_: SimpleNamespace(
        stream_complete=lambda *a, **k: prompt_completions.pop(0)))
    resp_prompt = agent.run_turn("保存归档笔记", session_id="scheduler-prompt-custom-1-2026-11-26")
    assert resp_prompt.degraded


def test_promotion_excludes_recall_echoes_and_runs_quality_gate_after_refinement(tmp_path: Path):
    assert not should_capture("search_memory", '{"query":"300406 九强生物","results":[{"content":"涨停12家"}]}')
    assert not should_capture("load_skill", "涨停策略说明")
    assert not should_capture("recall_timeline", "涨停池锁定12")

    obs_store = ObservationStore(tmp_path / "observations.db")
    # Flood store with 30 search_memory echoes AND 60 (> limit=50) raw [tool] k=v; k=v headers
    for i in range(30):
        obs_store.append(f"echo-{i}", "search_memory", f"[search_memory] query=300406 九强生物 {i}", importance=9, concepts=["300406"])
    for i in range(60):
        obs_store.append(f"kv-{i}", "get_market_pulse", f"[get_market_pulse] timestamp=2026-11-26; breadth={i}; sectors=电子", importance=9, concepts=["电子"])
    for o in obs_store.recent(limit=100):
        obs_store.mark_consolidated([o.id])
        for d in range(6):
            obs_store.record_recall([o.id], session_id=f"s-{d}")

    # Add 2 genuine market/signal observations that carry concrete dates/prices in raw summary
    obs_store.append("real-1", "emit_signal", '{"symbol":"002042","rating":"buy","entry_price":3.99,"date":"2026-11-26"} 回踩MA20企稳缩量后放量', importance=8, concepts=["回踩", "MA20"])
    obs_store.append("real-2", "analyze_portfolio", "今天收盘价站稳MA20，回踩企稳案例验证有效", importance=8, concepts=["回踩", "MA20"])
    for o in obs_store.recent(limit=5):
        if o.tool_name in {"emit_signal", "analyze_portfolio"}:
            obs_store.mark_consolidated([o.id])
            obs_store.record_recall([o.id], session_id="sa")
            obs_store.record_recall([o.id], session_id="sb")
            obs_store.bump_daily([o.id])

    skill_store = SkillStore(tmp_path / "memory" / "skills")
    mem_store = MemoryStore(tmp_path / "memory", write_gate=SemanticWriteGate(skill_store=skill_store))
    ranked = rank_promotion_candidates(obs_store, min_signal=3, limit=50, skill_store=skill_store)
    assert len(ranked) == 2
    assert all(c.observation.tool_name != "search_memory" for c in ranked)

    # Also verify unclosed <think> is safely skipped rather than written to KNOWLEDGE.md
    from trade_compass_agent.memory.promotion import _strip_reasoning_tags
    assert _strip_reasoning_tags("<think>未闭合的思考链内容") == ""

    def fake_llm(system: str, user: str) -> str:
        if "记忆提炼引擎" in system:
            return "<think>先去除日期与价格</think>趋势股回踩MA20缩量企稳并伴随板块资金回流时分批建仓胜率更高。"
        return json.dumps({
            "verdict": "KNOWLEDGE",
            "reason": "具备两次独立案例支撑且去除具体日期价格",
            "refined": "趋势股回踩MA20缩量企稳并伴随板块资金回流时，优先于追高突破分批建仓。",
        }, ensure_ascii=False)

    results = apply_promotions(ranked, mem_store, obs_store, threshold=0.3, llm_call=fake_llm, skill_store=skill_store)
    assert len(results) == 1 and results[0].verdict == "KNOWLEDGE"
    assert len(mem_store.list_active("memory")) == 1


def test_workflow_step_snapshots_resolve_deferred_reflection_and_historical_migration_preview(tmp_path: Path, monkeypatch):
    config = AppConfig(data_dir=tmp_path / "data", memory_dir=tmp_path / "memory", data_provider="sample")
    pos = SimpleNamespace(symbol="002042", account=AccountKind.SHORT_STOCK, price_is_fresh=True, avg_cost=3.99, last_price=4.32)
    monkeypatch.setattr(
        "trade_compass_agent.portfolio.JsonPaperPortfolio",
        lambda *a, **k: SimpleNamespace(positions_with_market_prices=lambda provider: [pos]),
    )
    monkeypatch.setattr(
        "trade_compass_agent.runtime.market_stack.MarketStack.from_config",
        lambda cfg: SimpleNamespace(provider=SimpleNamespace()),
    )

    pending = PendingReflection(
        job_id="close",
        run_id="r-close",
        run_date="2026-11-26",
        predictions={
            "workflow": {
                "workflow_id": "close_check",
                "step_snapshots": {
                    "mark_to_market": {
                        "positions": [{"symbol": "002042", "account": "short_stock", "pnl_pct": 0.0, "price_is_fresh": True}]
                    },
                    "exit_check": {"alerts": []},
                },
            }
        },
        summary="close_check: 收盘分析",
    )
    resolved = resolve_pending_with_market(pending, config, as_of=date(2026, 11, 27))
    assert resolved is not None
    actuals, lesson = resolved
    assert "无持仓快照" not in lesson
    assert actuals["positions"][0]["symbol"] == "002042"
    assert actuals["positions"][0]["actual_pnl_pct"] > 8.0


def test_historical_migration_preview_and_apply(tmp_path: Path):
    # Historical migration: preview leaves files untouched; apply links unambiguous 1-to-1 and skips ambiguous
    mig_dir = tmp_path / "mig_data"
    mig_dir.mkdir()
    tracker = SignalTracker(mig_dir)
    tracker.track_signal({"signal_id": "sig-601598", "symbol": "601598", "rating": "buy", "confidence": 0.8,
                          "timestamp": "2026-11-19T09:15:00", "account": "short_stock"})
    tracker.track_signal({"signal_id": "sig-hold", "symbol": "600519", "rating": "hold", "confidence": 0.6,
                          "timestamp": "2026-11-10T09:15:00"})
    # Force legacy 'pending' on hold to simulate pre-fix tracking file
    rows = tracker._load_all()
    for r in rows:
        r.status = "pending"
    tracker._save_all(rows)

    real_portfolio = JsonPaperPortfolio(mig_dir / "paper_trades.jsonl")
    real_portfolio.record(PaperTrade(
        symbol="601598", account=AccountKind.SHORT_STOCK, side="buy", quantity=1000,
        price=7.00, timestamp=datetime(2026, 11, 19, 10, 5), reason="历史买入", trade_id="buy-601598",
    ))
    real_portfolio.record(PaperTrade(
        symbol="601598", account=AccountKind.SHORT_STOCK, side="sell", quantity=1000,
        price=7.13, timestamp=datetime(2026, 11, 23, 9, 36), reason="历史卖出", trade_id="sell-601598",
    ))

    before_trades = (mig_dir / "paper_trades.jsonl").read_bytes()
    preview = reconcile_historical_signals(mig_dir, apply=False, as_of=date(2026, 11, 28))
    assert preview["mode"] == "preview"
    assert preview["unambiguous_matches_count"] == 1
    assert preview["non_entry_pending_to_observed"] == 1
    assert (mig_dir / "paper_trades.jsonl").read_bytes() == before_trades

    applied = reconcile_historical_signals(mig_dir, apply=True, as_of=date(2026, 11, 28))
    assert applied["mode"] == "applied" and Path(applied["backup_dir"]).is_dir()
    closed = tracker.get_closed(outcome_basis="execution_net_pnl")
    assert len(closed) == 1 and closed[0].signal_id == "sig-601598"
    assert closed[0].attribution_status == "migrated_historical"


def test_eastmoney_board_secondary_host_recovery_and_weixin_poll_diagnostics(tmp_path: Path, monkeypatch, caplog):
    from http.client import RemoteDisconnected
    import httpx
    from trade_compass_agent.channels.weixin import WeixinBotAdapter
    from trade_compass_agent.data import sector_boards

    monkeypatch.setattr(sector_boards, "rate_limit_domain", lambda _url: None)
    calls: list[str] = []

    def fake_get(url: str, **kwargs):
        calls.append(url)
        if "17.push2.eastmoney.com" in url:
            raise RemoteDisconnected("Remote end closed connection without response")
        return SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {"data": {"diff": [{"f14": "半导体", "f3": 3.25, "f128": "北方华创"}]}},
        )

    monkeypatch.setattr("requests.get", fake_get)
    rows = sector_boards.fetch_eastmoney_board_rows(board_type="industry", limit=5)
    assert len(rows) == 1 and rows[0]["f14"] == "半导体"
    assert len(calls) == 2 and "push2.eastmoney.com" in calls[1]

    # WeixinBotAdapter: ReadTimeout does not emit empty warning; ConnectError("") logs class name
    cred = tmp_path / "weixin.json"
    cred.write_text('{"token":"tok","bot_id":"b1"}', encoding="utf-8")
    adapter = WeixinBotAdapter(credentials_path=str(cred))
    poll_events = [httpx.ReadTimeout(""), httpx.ConnectError("")]

    async def fake_poll():
        if poll_events:
            err = poll_events.pop(0)
            if isinstance(err, httpx.ConnectError):
                adapter._running = False
            raise err
        adapter._running = False
        return []

    monkeypatch.setattr(adapter, "_poll_updates", fake_poll)
    async def fast_sleep(_d):
        return None
    monkeypatch.setattr("asyncio.sleep", fast_sleep)
    with caplog.at_level("WARNING"):
        asyncio.run(adapter.start_listening())
    warnings = [r.message for r in caplog.records]
    assert len(warnings) == 1
    assert "ConnectError" in warnings[0] and "error: ," not in warnings[0]


def test_review_counterexamples_cache_gap_premarket_reflection_and_utc_migration(tmp_path: Path, monkeypatch):
    # 1) Short later bar cache (starting Oct 30) must NOT fabricate returns for an Oct 15 signal
    data_dir = tmp_path / "data"
    mem_dir = tmp_path / "memory"
    cache = LocalBarCacheProvider(data_dir / "market_cache", allow_historical=True)
    later_bars = [
        Bar(symbol="002042", timestamp=datetime(2026, 10, 30) + timedelta(days=i), open=4.0, high=4.2, low=3.9, close=4.0 + i * 0.05, volume=10000)
        for i in range(10)
    ]
    cache.write_bars("002042", "1d", later_bars, requested_limit=10)
    audit = JsonAuditLog(data_dir / "audit.jsonl")
    ev_row = audit.record(
        event_type="trading_signal",
        summary="002042 buy",
        payload={"signal_id": "sig-oct15", "symbol": "002042", "rating": "buy", "source_skills": ["intraday-tech"]},
    )
    audit_lines = [json.loads(line) for line in (data_dir / "audit.jsonl").read_text(encoding="utf-8").splitlines()]
    audit_lines[0]["timestamp"] = "2026-10-15T10:00:00"
    (data_dir / "audit.jsonl").write_text(json.dumps(audit_lines[0]) + "\n", encoding="utf-8")

    ft = FollowThroughEvaluator(cache).evaluate(JsonAuditLog(data_dir / "audit.jsonl").events, limit=10)
    assert len(ft.results) == 1 and ft.results[0].audit_id == ev_row.id
    assert ft.results[0].status == "no_entry_bar" and ft.results[0].return_1d is None
    assert any("cached bars start at 2026-10-30" in w for w in ft.warnings)

    # 2) Premarket morning_plan snapshot with price_is_fresh=False drains cleanly with zero market fetches
    config = AppConfig(data_dir=data_dir, memory_dir=mem_dir, data_provider="sample")
    monkeypatch.setattr(
        "trade_compass_agent.runtime.market_stack.MarketStack.from_config",
        lambda _cfg: (_ for _ in ()).throw(AssertionError("must not fetch live market for non-fresh premarket snapshot")),
    )
    mp_pending = PendingReflection(
        job_id="morning_plan",
        run_id="r-mp",
        run_date="2026-11-26",
        predictions={
            "workflow": {
                "step_snapshots": {
                    "portfolio_check": {
                        "positions": [{"symbol": "002042", "account": "short_stock", "price_is_fresh": False, "pnl_pct": 0.0}]
                    }
                }
            }
        },
        summary="morning_plan: 晨间计划",
    )
    mp_res = resolve_pending_with_market(mp_pending, config, as_of=date(2026, 11, 27))
    assert mp_res is not None and "盘前无实时估值基线" in mp_res[1]

    # 3) UTC signal at 02:00Z (= 10:00 CST) must NOT match a 09:35 CST buy; empty rating "" must NOT match
    mig_dir = tmp_path / "utc_mig"
    mig_dir.mkdir()
    tracker = SignalTracker(mig_dir)
    tracker.track_signal({"signal_id": "sig-after-buy-utc", "symbol": "002042", "rating": "buy", "confidence": 0.8,
                          "timestamp": "2026-11-19T02:00:00Z", "account": "short_stock"})
    tracker.track_signal({"signal_id": "sig-empty-rating", "symbol": "002042", "rating": "", "confidence": 0.5,
                          "timestamp": "2026-11-19T09:10:00", "account": "short_stock"})
    rows = tracker._load_all()
    for r in rows:
        r.status = "pending"
    tracker._save_all(rows)
    pf = JsonPaperPortfolio(mig_dir / "paper_trades.jsonl")
    pf.record(PaperTrade(
        symbol="002042", account=AccountKind.SHORT_STOCK, side="buy", quantity=1000,
        price=3.99, timestamp=datetime(2026, 11, 19, 9, 35), reason="早盘买入", trade_id="buy-0935",
    ))
    preview = reconcile_historical_signals(mig_dir, apply=False, as_of=date(2026, 11, 28))
    assert preview["unambiguous_matches_count"] == 0
    assert preview["non_entry_pending_to_observed"] == 1
