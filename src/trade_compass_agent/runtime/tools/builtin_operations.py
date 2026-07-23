"""Built-in Trade Compass operations.

Each function is an async operation handler with signature:
    async def handler(ctx: StepContext) -> StepOutput

Deterministic operations do pure computation; agent operations invoke
ScheduledAgentSession through the shared agent-session runner.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import timedelta

from trade_compass_agent.ops.agent_session import run_agent_step
from trade_compass_agent.ops.job_definition import StepContext, StepOutput
from trade_compass_agent.runtime.tools.artifact_tracking import update_artifact_tracking

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════
# premarket operations
# ═══════════════════════════════════════════════════════════════════════════

async def scan_portfolio_exits(ctx: StepContext) -> StepOutput:
    """Scan positions for exit signals using threshold rules."""
    _, positions = _load_portfolio_mtm(ctx.config)
    signal_map = _load_signal_map(ctx.config)
    alerts: list[str] = []
    for p in positions:
        alerts.extend(_exit_alerts_for_position(p, signal_map))

    positions_detail = []
    for p in positions:
        pnl_pct = _position_pnl_pct(p)
        positions_detail.append({
            "symbol": p.symbol, "quantity": p.quantity,
            "avg_cost": p.avg_cost, "last_price": p.last_price,
            "pnl_pct": round(pnl_pct, 2),
        })

    return StepOutput(
        message=f"{len(positions)}个持仓, {len(alerts)}条预警",
        data={"positions": positions_detail, "alerts": alerts},
    )


async def reconcile_portfolio_memory(ctx: StepContext) -> StepOutput:
    """Compare OMS positions against instrument pages to detect drift."""
    import re
    from trade_compass_agent.portfolio import JsonPaperPortfolio

    portfolio = JsonPaperPortfolio(
        ctx.config.data_dir / "paper_trades.jsonl",
        costs=ctx.config.trading_costs,
    )
    oms_positions = {p.symbol: p.quantity for p in portfolio.positions()}

    instruments_dir = ctx.config.memory_dir / "instruments"
    memory_held: dict[str, str] = {}
    if instruments_dir.is_dir():
        for md_file in instruments_dir.glob("*.md"):
            symbol = md_file.stem
            try:
                text = md_file.read_text(encoding="utf-8")
            except Exception:
                continue
            if re.search(r"已平仓|已清仓", text):
                continue
            if re.search(r"持仓\d+股|持有|成本价|短线账户持仓|综合成本", text):
                memory_held[symbol] = md_file.name

    discrepancies: list[str] = []
    for sym in sorted(set(oms_positions) | set(memory_held)):
        in_oms = sym in oms_positions
        in_mem = sym in memory_held
        if in_oms and not in_mem:
            discrepancies.append(f"{sym}: OMS持仓{oms_positions[sym]}股，但无instrument页面")
        elif in_mem and not in_oms:
            discrepancies.append(f"{sym}: instrument页面标记持有，但OMS无持仓（可能已清仓未同步）")
        # quantity mismatch is harder to detect from markdown; skip for now

    if not discrepancies:
        return StepOutput(message="持仓与记忆一致", data={"discrepancies": []})

    return StepOutput(
        message=f"发现{len(discrepancies)}处持仓-记忆不一致",
        data={"discrepancies": discrepancies},
    )


async def scan_overnight_news(ctx: StepContext) -> StepOutput:
    """Agent operation: scan overnight news/announcements for held stocks."""
    from trade_compass_agent.portfolio import JsonPaperPortfolio

    portfolio = JsonPaperPortfolio(
        ctx.config.data_dir / "paper_trades.jsonl",
        costs=ctx.config.trading_costs,
    )
    symbols = [p.symbol for p in portfolio.positions()]
    if not symbols:
        return StepOutput(message="空仓，无需扫描隔夜新闻", data={"summary": "空仓"})

    prompt = (
        f"请搜索以下 A 股标的的最新新闻和公告（昨日收盘后至今）：{', '.join(symbols)}\n"
        "重点关注：业绩预告、重大合同、股东增减持、监管处罚、行业政策变化。\n"
        "请简要总结每只股票的关键信息。如无重大消息，标注'无重大消息'。"
    )
    return await run_agent_step(ctx, prompt, "premarket", step_id="overnight_news")


async def check_global_markets(ctx: StepContext) -> StepOutput:
    """Agent operation: analyze global market impact on A-shares."""
    prompt = (
        f"今天是 {ctx.date.isoformat()}，请分析全球市场对今日 A 股的影响：\n"
        "1. 美股三大指数昨日收盘情况\n"
        "2. 港股恒生指数表现\n"
        "3. 人民币汇率变动\n"
        "4. 大宗商品（原油、黄金）走势\n"
        "5. 综合判断对 A 股今日开盘的影响（利好/利空/中性）\n"
        "请简洁输出，每项 1-2 句话。"
    )
    return await run_agent_step(ctx, prompt, "premarket", step_id="global_market")


async def agent_premarket_briefing(ctx: StepContext) -> StepOutput:
    """Agent operation: synthesize all premarket data into actionable briefing."""
    portfolio = ctx.upstream.get("portfolio_scan", StepOutput.empty())
    news = ctx.upstream.get("overnight_news", StepOutput.empty())
    catalysts = ctx.upstream.get("catalyst_calendar", StepOutput.empty())
    global_mkt = ctx.upstream.get("global_market", StepOutput.empty())

    prompt = (
        f"今天是 {ctx.date.isoformat()}，请基于以下信息给出今日盘前操作建议：\n\n"
        f"{_reflection_section(ctx)}"
        f"## 持仓预警\n{json.dumps(portfolio.data.get('alerts', []), ensure_ascii=False)}\n"
        f"## 持仓详情\n{json.dumps(portfolio.data.get('positions', []), ensure_ascii=False)}\n\n"
        f"## 隔夜新闻\n{news.data.get('analysis', '未获取')}\n\n"
        f"## 催化剂日历\n{json.dumps(catalysts.data, ensure_ascii=False, indent=2)}\n\n"
        f"## 全球市场\n{global_mkt.data.get('analysis', '未获取')}\n\n"
        "请输出：1) 今日关注事项 2) 持仓操作建议（加/减/持有/观望） 3) 风险提示"
    )
    return await run_agent_step(ctx, prompt, "premarket", step_id="agent_briefing")


# ═══════════════════════════════════════════════════════════════════════════
# morning_plan operations
# ═══════════════════════════════════════════════════════════════════════════

async def run_screening_engine(ctx: StepContext) -> StepOutput:
    """L1-L4 screening engine. Pure computation, no LLM."""
    return await asyncio.to_thread(_run_screening_engine_sync, ctx)


def _run_screening_engine_sync(ctx: StepContext) -> StepOutput:
    try:
        from trade_compass_agent.screening.config import ScreeningConfig
        from trade_compass_agent.screening.universe import resolve_universe, filter_st
        from trade_compass_agent.screening.engine import run_screening
        from trade_compass_agent.data.providers import create_bulk_daily_provider
        from trade_compass_agent.data.fund_flow import FundFlowProvider
        import time
        import pandas as pd

        cfg = ScreeningConfig.from_env()
        stocks = resolve_universe(cfg.boards)
        if not stocks:
            return StepOutput(message="无法获取股票列表", data={"error": "无法获取股票列表", "candidates": []})

        if cfg.exclude_st:
            stocks = filter_st(stocks)

        provider = create_bulk_daily_provider(
            cache_dir=ctx.config.data_dir / "market_cache",
        )
        df_map: dict[str, pd.DataFrame] = {}
        symbols = [s.symbol for s in stocks]

        def _fetch_one(symbol: str) -> tuple[str, pd.DataFrame | None]:
            try:
                bars = provider.get_bars(symbol, timeframe="1d", limit=cfg.trading_days)
                if bars:
                    return symbol, pd.DataFrame([
                        {"open": b.open, "high": b.high, "low": b.low,
                         "close": b.close, "volume": b.volume,
                         "amount": getattr(b, "amount", b.volume * b.close)}
                        for b in bars
                    ])
            except Exception:
                pass
            return symbol, None

        from concurrent.futures import ThreadPoolExecutor, as_completed
        max_workers = min(cfg.fetch_workers, len(symbols))
        t0 = time.monotonic()
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = [pool.submit(_fetch_one, s) for s in symbols]
            done = 0
            for fut in as_completed(futures):
                sym, df = fut.result()
                if df is not None:
                    df_map[sym] = df
                done += 1
                if done % 500 == 0:
                    logger.info("Screening data fetch: %d/%d (%.0fs)", done, len(symbols), time.monotonic() - t0)
        logger.info("Screening data fetch complete: %d/%d symbols in %.1fs", len(df_map), len(symbols), time.monotonic() - t0)

        hot_industries: list[str] = []
        hot_concepts: list[str] = []
        try:
            fp = FundFlowProvider()
            hot_industries = [f.sector_name for f in fp.get_sector_flow(category="industry", limit=5) if f.net_inflow > 0]
            hot_concepts = [f.sector_name for f in fp.get_sector_flow(category="concept", limit=5) if f.net_inflow > 0]
        except Exception as exc:
            logger.warning("Failed to fetch hot sectors: %s", exc)

        result = run_screening(df_map, cfg, stocks=stocks, hot_industries=hot_industries, hot_concepts=hot_concepts)
        candidates = [{"symbol": s.symbol, "score": s.composite} for s in result.top_n]

        return StepOutput(
            message=f"全市场{result.universe_size}只 → L1通过{result.l1_passed} → 评分{result.scored_count}只 → Top {len(candidates)}",
            data={
                "universe": result.universe_size, "l1_passed": result.l1_passed,
                "scored": result.scored_count, "candidates": candidates,
            },
        )
    except Exception as exc:
        logger.warning("Screening engine failed: %s", exc)
        return StepOutput(message=f"选股引擎失败: {exc}", data={"error": str(exc), "candidates": []})


async def run_l5_screener(ctx: StepContext) -> StepOutput:
    """Agent operation: L5 AI screener on top candidates."""
    screening = ctx.upstream.get("screening", StepOutput.empty())
    candidates = screening.data.get("candidates", [])
    if not candidates:
        return StepOutput(
            message="无候选，跳过 L5",
            data={
                "signals_emitted": 0,
                "signals": [],
                "summary": _summarize_l5_signals([], []),
            },
        )

    symbols = [c["symbol"] for c in candidates[:10]]

    try:
        from trade_compass_agent.evaluation.signal_tracker import SignalTracker
        from trade_compass_agent.runtime.market_stack import MarketStack
        from trade_compass_agent.runtime.specialists.run import run_specialist
        from trade_compass_agent.runtime.specialists.signal_parsing import parse_screener_signals

        stack = MarketStack.from_config(ctx.config)
        task = (
            f"## 候选列表（共 {len(symbols)} 只）\n"
            "请依次分析以下候选股票，使用工具获取数据后给出结构化评级：\n"
            + ", ".join(symbols)
        )
        report = await asyncio.to_thread(
            run_specialist,
            stack,
            "screener",
            task,
            config=ctx.config,
        )
        signals = parse_screener_signals(report, symbols)
        if not signals:
            return StepOutput(
                message="L5 审判未产出信号",
                data={
                    "signals_emitted": 0,
                    "signals": [],
                    "summary": _summarize_l5_signals([], candidates[:10]),
                    "report_excerpt": _truncate_text(report, 1600),
                },
            )

        tracker = SignalTracker(ctx.config.data_dir)
        signals_path = ctx.config.data_dir / "signals.jsonl"
        signals_path.parent.mkdir(parents=True, exist_ok=True)

        for signal in signals:
            with signals_path.open("a", encoding="utf-8") as f:
                f.write(signal.model_dump_json() + "\n")
            tracker.track_signal(signal.model_dump())

        return StepOutput(
            message=f"L5 审判产出 {len(signals)} 条信号",
            data={
                "signals_emitted": len(signals),
                "signals": [_signal_payload(signal, candidates) for signal in signals],
                "summary": _summarize_l5_signals(signals, candidates[:10]),
                "report_excerpt": _truncate_text(report, 1600),
            },
        )
    except Exception as exc:
        logger.warning("L5 screener failed: %s", exc)
        raise


def _signal_payload(signal, candidates: list[dict]) -> dict:
    payload = signal.model_dump(mode="json")
    score_by_symbol = {
        str(candidate.get("symbol")): candidate.get("score")
        for candidate in candidates
        if isinstance(candidate, dict)
    }
    if signal.symbol in score_by_symbol:
        payload["screening_score"] = score_by_symbol[signal.symbol]
    return payload


def _summarize_l5_signals(signals, candidates: list[dict]) -> dict:
    by_rating = {rating: 0 for rating in ("strong_buy", "buy", "hold", "sell", "strong_sell")}
    top_signals = []
    for signal in sorted(signals, key=lambda item: item.confidence, reverse=True):
        rating = signal.rating.value
        by_rating[rating] = by_rating.get(rating, 0) + 1
        top_signals.append(
            {
                "symbol": signal.symbol,
                "rating": rating,
                "confidence": signal.confidence,
                "reason": _truncate_text(signal.reasoning, 180),
                "entry_price": signal.entry_price,
                "stop_loss": signal.stop_loss,
                "target_price": signal.target_price,
                "risk_reward_ratio": signal.risk_reward_ratio,
            }
        )
    covered = {signal.symbol for signal in signals}
    candidate_symbols = [str(item.get("symbol")) for item in candidates if isinstance(item, dict) and item.get("symbol")]
    missing = [symbol for symbol in candidate_symbols if symbol not in covered]
    return {
        "status": "ok" if signals else "no_signal",
        "total": len(signals),
        "by_rating": by_rating,
        "top_signals": top_signals[:10],
        "actionable_buys": [
            item["symbol"]
            for item in top_signals
            if item["rating"] in {"strong_buy", "buy"}
        ][:5],
        "risk_or_avoid": [
            item["symbol"]
            for item in top_signals
            if item["rating"] in {"sell", "strong_sell"}
        ][:5],
        "uncovered_candidates": missing[:10],
    }


def _truncate_text(text: str | None, limit: int) -> str:
    value = str(text or "").strip()
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "…"


async def check_positions(ctx: StepContext) -> StepOutput:
    """Check current positions and P&L. Pure computation."""
    _, positions = _load_portfolio_mtm(ctx.config)
    detail = []
    for p in positions:
        detail.append({
            "symbol": p.symbol, "quantity": p.quantity,
            "avg_cost": p.avg_cost, "last_price": p.last_price,
            "price_source": p.price_source,
            "price_is_fresh": p.price_is_fresh,
            "pnl_pct": round(_position_pnl_pct(p), 2),
        })

    stale_symbols = [p.symbol for p in positions if not p.price_is_fresh]
    return StepOutput(
        message=f"{len(positions)}个持仓" if positions else "空仓",
        data={
            "positions": detail,
            "count": len(positions),
            "warnings": [
                f"{len(stale_symbols)}个持仓未获取到实时/最新行情: {', '.join(stale_symbols[:20])}"
            ] if stale_symbols else [],
        },
    )


async def scan_sector_capital_flow(ctx: StepContext) -> StepOutput:
    """Agent operation: analyze sector capital flows."""
    prompt = (
        f"今天是 {ctx.date.isoformat()}，请分析 A 股市场板块资金流向：\n"
        "1. 资金净流入 Top 5 行业板块\n"
        "2. 资金净流入 Top 5 概念板块\n"
        "3. 主力资金流向趋势（与昨日对比）\n"
        "4. 判断今日的热点方向和值得关注的板块"
    )
    return await run_agent_step(ctx, prompt, "morning_plan", step_id="sector_flow")


async def agent_morning_plan(ctx: StepContext) -> StepOutput:
    """Agent operation: synthesize screening + positions + sectors into trading plan."""
    screening = ctx.upstream.get("screening", StepOutput.empty())
    screener_ai = ctx.upstream.get("screener_ai", StepOutput.empty())
    positions = ctx.upstream.get("portfolio_check", StepOutput.empty())
    sector = ctx.upstream.get("sector_flow", StepOutput.empty())
    ideas = ctx.upstream.get("idea_generation", StepOutput.empty())
    risk = ctx.upstream.get("risk_review", StepOutput.empty())

    candidates = screening.data.get("candidates", [])
    decision_context = _build_morning_decision_context(
        candidates=candidates,
        l5_data=screener_ai.data,
        positions_data=positions.data,
        sector_data=sector.data,
        ideas_data=ideas.data,
        risk_data=risk.data,
    )

    prompt = (
        f"今天是 {ctx.date.isoformat()}，请基于下方 decision_context 生成今日 A 股交易计划。\n\n"
        f"{_reflection_section(ctx)}"
        "## 执行边界\n"
        "- 这是最终计划合成步骤；decision_context 是初始上下文，不是唯一依据。\n"
        "- 如判断需要最新数据，可调用可用数据工具补齐，例如持仓最新价、K线/均线/RSI、组合盈亏、候选入场价与风险约束。\n"
        "- 若持仓 price_is_fresh=false，可调用 analyze_portfolio 或 sina_realtime_quote/get_bars 核实后再判断盈亏；无法核实时写入「数据缺口」。\n"
        "- 禁止把 price_source=last_trade 或 avg_cost_fallback 的 last_price 解读为最新价；这种情况下不得写「成本价持平」。\n"
        "- 禁止调用下单/交易执行工具；本步骤只产出计划、信号和记忆记录。\n"
        "- 可以调用 emit_signal 记录最终结构化信号，可以调用 write_memory 记录交易计划摘要。\n"
        "- 输出必须聚焦：持仓动作、新建仓候选、观察清单、风险约束、数据缺口。\n\n"
        "## decision_context\n"
        f"{json.dumps(decision_context, ensure_ascii=False, indent=2)}\n\n"
        "请输出：1) 今日核心结论 2) 持仓操作建议 3) 新建仓/观察候选 4) 风险提示 5) 数据缺口。"
    )
    return await run_agent_step(
        ctx,
        prompt,
        "morning_plan",
        step_id="agent_plan",
        tool_whitelist=_MORNING_PLAN_TOOL_WHITELIST,
    )


_MORNING_PLAN_TOOL_WHITELIST = {
    "analyze_portfolio",
    "batch_get_bars",
    "batch_get_fundamentals",
    "batch_search_news",
    "chart_pattern",
    "check_exit_signals",
    "compute_bollinger",
    "compute_ma",
    "compute_macd",
    "compute_rsi",
    "compute_volume_ratio",
    "dispatch_specialists",
    "eastmoney_news",
    "emit_signal",
    "fetch_url",
    "get_bars",
    "get_block_trades",
    "get_chip_distribution",
    "get_events",
    "get_fund_flow",
    "get_fundamentals",
    "get_institutional_holdings",
    "get_margin_data",
    "get_market_constraints",
    "get_market_pulse",
    "get_shareholder_structure",
    "load_skill",
    "search_announcements",
    "search_concept_boards",
    "search_hot_stocks",
    "search_industry_boards",
    "search_institute_recommend",
    "search_lhb",
    "search_market_activity",
    "search_market_flash",
    "search_memory",
    "search_research_reports",
    "search_stock_news",
    "search_x",
    "search_x_kol",
    "search_xueqiu_hot",
    "sina_realtime_quote",
    "web_search",
    "write_memory",
}


def _build_morning_decision_context(
    *,
    candidates: list,
    l5_data: dict,
    positions_data: dict,
    sector_data: dict,
    ideas_data: dict,
    risk_data: dict,
) -> dict:
    l5_signals = l5_data.get("signals") if isinstance(l5_data, dict) else []
    l5_summary = l5_data.get("summary") if isinstance(l5_data, dict) else {}
    return {
        "candidate_snapshot": [
            {
                "symbol": str(item.get("symbol") or ""),
                "score": item.get("score"),
            }
            for item in candidates[:10]
            if isinstance(item, dict)
        ],
        "l5_summary": l5_summary if isinstance(l5_summary, dict) else {},
        "l5_top_signals": _compact_l5_signals(l5_signals),
        "positions": _compact_positions(positions_data.get("positions") if isinstance(positions_data, dict) else []),
        "sector_context": {
            "message": _truncate_text(sector_data.get("message") if isinstance(sector_data, dict) else "", 300),
            "analysis_excerpt": _truncate_text(sector_data.get("analysis") if isinstance(sector_data, dict) else "", 1200),
        },
        "ideas": _compact_ideas(ideas_data),
        "risk_review": _compact_risk_review(risk_data),
    }


def _compact_l5_signals(raw: object, limit: int = 10) -> list[dict]:
    if not isinstance(raw, list):
        return []
    signals: list[dict] = []
    for item in raw[:limit]:
        if not isinstance(item, dict):
            continue
        signals.append(
            {
                "symbol": item.get("symbol"),
                "rating": item.get("rating"),
                "confidence": item.get("confidence"),
                "screening_score": item.get("screening_score"),
                "entry_price": item.get("entry_price"),
                "stop_loss": item.get("stop_loss"),
                "target_price": item.get("target_price"),
                "risk_reward_ratio": item.get("risk_reward_ratio"),
                "reasoning": _truncate_text(item.get("reasoning"), 220),
            }
        )
    return signals


def _compact_positions(raw: object, limit: int = 25) -> list[dict]:
    if not isinstance(raw, list):
        return []
    positions: list[dict] = []
    for item in raw[:limit]:
        if not isinstance(item, dict):
            continue
        positions.append(
            {
                "symbol": item.get("symbol"),
                "quantity": item.get("quantity"),
                "avg_cost": item.get("avg_cost"),
                "last_price": item.get("last_price"),
                "price_source": item.get("price_source"),
                "price_is_fresh": item.get("price_is_fresh"),
                "pnl_pct": item.get("pnl_pct"),
            }
        )
    return positions


def _compact_ideas(raw: dict) -> dict:
    if not isinstance(raw, dict):
        return {"ideas": [], "warnings": []}
    ideas = raw.get("ideas")
    compact: list[dict] = []
    if isinstance(ideas, list):
        for item in ideas[:10]:
            if not isinstance(item, dict):
                continue
            compact.append(
                {
                    "symbol": item.get("symbol"),
                    "direction": item.get("direction"),
                    "score": item.get("score"),
                    "drivers": item.get("drivers") or [],
                    "risks": item.get("risks") or [],
                    "next_step": item.get("next_step"),
                }
            )
    return {
        "ideas": compact,
        "warnings": raw.get("warnings") if isinstance(raw.get("warnings"), list) else [],
    }


def _compact_risk_review(raw: dict) -> dict:
    if not isinstance(raw, dict):
        return {"warnings": []}
    metadata = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {}
    return {
        "metadata": metadata,
        "report_excerpt": _truncate_text(raw.get("report_markdown"), 1400),
        "warnings": raw.get("warnings") if isinstance(raw.get("warnings"), list) else [],
    }


# ═══════════════════════════════════════════════════════════════════════════
# close steps
# ═══════════════════════════════════════════════════════════════════════════

async def refresh_market_prices(ctx: StepContext) -> StepOutput:
    """Mark-to-market: refresh position prices."""
    from trade_compass_agent.portfolio import JsonPaperPortfolio
    from trade_compass_agent.runtime.market_stack import MarketStack

    portfolio = JsonPaperPortfolio(
        ctx.config.data_dir / "paper_trades.jsonl",
        costs=ctx.config.trading_costs,
    )
    stack = MarketStack.from_config(ctx.config)
    positions = portfolio.positions_with_market_prices(stack.provider)

    total_value = sum(p.market_value for p in positions)
    total_unrealized = sum(p.unrealized_pnl for p in positions)

    detail = []
    for p in positions:
        pnl_pct = (p.last_price / p.avg_cost - 1) * 100 if p.avg_cost > 0 else 0
        detail.append({
            "symbol": p.symbol, "quantity": p.quantity,
            "market_value": round(p.market_value, 2),
            "unrealized_pnl": round(p.unrealized_pnl, 2),
            "pnl_pct": round(pnl_pct, 2),
        })

    return StepOutput(
        message=f"{len(positions)}个持仓, 总市值{total_value:.0f}, P&L {total_unrealized:.0f}",
        data={"positions": detail, "total_value": total_value, "total_unrealized": total_unrealized},
    )


async def check_exit_signals(ctx: StepContext) -> StepOutput:
    """Check exit signals against refreshed prices."""
    from trade_compass_agent.portfolio.lot_sizing import format_pnl_alert

    mtm = ctx.upstream.get("mark_to_market", StepOutput.empty())
    signal_map = _load_signal_map(ctx.config)
    positions = mtm.data.get("positions", [])

    alerts: list[str] = []
    for p in positions:
        symbol = p["symbol"]
        pnl_pct = p.get("pnl_pct", 0)
        quantity = p.get("quantity", 0)
        sig = signal_map.get(symbol)
        if sig and sig.get("stop_loss") and p.get("market_value", 0) > 0:
            last_price = p.get("market_value", 0) / max(p.get("quantity", 1), 1)
            if last_price <= sig["stop_loss"]:
                alerts.append(f"{symbol} 触发止损(当前价≤{sig['stop_loss']})")
        lot_alert = format_pnl_alert(symbol, quantity, pnl_pct)
        if lot_alert:
            alerts.append(lot_alert)

    return StepOutput(
        message=f"{len(alerts)}条出场预警" if alerts else "无出场预警",
        data={"alerts": alerts},
    )


async def agent_close_analysis(ctx: StepContext) -> StepOutput:
    """Agent operation: analyze today's price action for held positions."""
    mtm = ctx.upstream.get("mark_to_market", StepOutput.empty())
    exits = ctx.upstream.get("exit_check", StepOutput.empty())

    prompt = (
        f"收盘分析 {ctx.date.isoformat()}。请判断持仓逻辑是否仍成立：\n\n"
        f"{_reflection_section(ctx)}"
        f"## 持仓估值\n{json.dumps(mtm.data.get('positions', []), ensure_ascii=False, indent=2)}\n\n"
        f"## 出场信号\n{json.dumps(exits.data.get('alerts', []), ensure_ascii=False)}\n\n"
        "请分析：1) 每个持仓今日走势要点 2) 持仓逻辑是否有变化 3) 明日关注点"
    )
    return await run_agent_step(ctx, prompt, "close", step_id="agent_close_analysis")


# ═══════════════════════════════════════════════════════════════════════════
# eod_review steps
# ═══════════════════════════════════════════════════════════════════════════

async def review_pnl(ctx: StepContext) -> StepOutput:
    """Review P&L for all positions."""
    from trade_compass_agent.portfolio.lot_sizing import format_pnl_alert

    _, positions = _load_portfolio_mtm(ctx.config)
    total_unrealized = sum(p.unrealized_pnl for p in positions)
    total_value = sum(p.market_value for p in positions)

    alerts: list[str] = []
    detail = []
    for p in positions:
        pnl_pct = _position_pnl_pct(p)
        detail.append({"symbol": p.symbol, "pnl_pct": round(pnl_pct, 2), "unrealized": round(p.unrealized_pnl, 2)})
        lot_alert = format_pnl_alert(p.symbol, p.quantity, pnl_pct)
        if lot_alert:
            alerts.append(lot_alert)

    return StepOutput(
        message=f"持仓{len(positions)}个, 总市值{total_value:.0f}, P&L {total_unrealized:.0f}",
        data={"positions": detail, "alerts": alerts, "total_value": total_value, "total_unrealized": total_unrealized},
    )


async def update_signal_tracker(ctx: StepContext) -> StepOutput:
    """Update signal tracker with current position data."""
    from trade_compass_agent.evaluation.signal_tracker import SignalTracker

    _, positions = _load_portfolio_mtm(ctx.config)
    tracker = SignalTracker(ctx.config.data_dir)
    active = tracker.get_active()

    held_symbols = {p.symbol for p in positions}
    position_map = {p.symbol: p for p in positions}
    updated = 0

    for signal in active:
        if signal.symbol in held_symbols:
            p = position_map[signal.symbol]
            if p.avg_cost > 0:
                current_pnl = _position_pnl_pct(p)
                signal.max_favorable = max(signal.max_favorable, current_pnl)
                signal.max_adverse = min(signal.max_adverse, current_pnl)
                updated += 1

    tracker._save_all(active + [s for s in tracker._load_all() if s.status != "active"])

    return StepOutput(
        message=f"信号追踪: {len(active)}条活跃, {updated}条已更新",
        data={"active_count": len(active), "updated": updated},
    )


async def sync_instrument_pages(ctx: StepContext) -> StepOutput:
    """Ensure markdown instrument pages exist and reflect the OMS trade ledger."""
    from collections import defaultdict

    from trade_compass_agent.memory.instrument_store import InstrumentStore
    from trade_compass_agent.portfolio import JsonPaperPortfolio

    portfolio = JsonPaperPortfolio(
        ctx.config.data_dir / "paper_trades.jsonl",
        costs=ctx.config.trading_costs,
    )
    trades_by_symbol = defaultdict(list)
    for trade in sorted(portfolio.trades, key=lambda item: item.timestamp):
        trades_by_symbol[trade.symbol].append(trade)

    if not trades_by_symbol:
        return StepOutput(message="无交易流水，无需同步个股档案", data={"created": 0, "updated": 0})

    store = InstrumentStore(ctx.config.memory_dir)
    created = 0
    updated = 0

    for symbol, trades in sorted(trades_by_symbol.items()):
        existed = store.exists(symbol)
        result = store.replace_trade_history(symbol, [_instrument_trade_entry(trade) for trade in trades])
        if not result.get("ok"):
            logger.warning("Instrument page sync failed for %s: %s", symbol, result.get("error"))
            continue
        if existed:
            updated += 1
        else:
            created += 1

    return StepOutput(
        message=f"个股档案同步完成: 新建{created}个, 更新{updated}个",
        data={"created": created, "updated": updated, "symbols": sorted(trades_by_symbol)},
    )


def _instrument_trade_entry(trade) -> str:
    account = trade.account.value if hasattr(trade.account, "value") else str(trade.account)
    entry = f"- {trade.timestamp:%Y-%m-%d} {trade.side} {trade.quantity}股 @{trade.price:g} [{account}]"
    if trade.reason:
        entry += f" ({trade.reason})"
    return entry


async def update_stock_profiles(ctx: StepContext) -> StepOutput:
    """Update stock profiles from recently closed trades."""
    from trade_compass_agent.config import settings_from_config
    from trade_compass_agent.memory.enhanced import EnhancedMemory
    from trade_compass_agent.portfolio import JsonPaperPortfolio

    settings = settings_from_config(ctx.config)
    portfolio = JsonPaperPortfolio(
        ctx.config.data_dir / "paper_trades.jsonl",
        costs=ctx.config.trading_costs,
    )
    realized = portfolio.realized_trades()
    if not realized:
        return StepOutput(message="无已平仓交易", data={"updated": 0})

    memory = EnhancedMemory(settings.memory_dir)
    today = ctx.date
    recent_trades = [r for r in realized if (today - r.closed_at.date()).days <= 1]
    updated = 0

    for trade in recent_trades:
        pnl_pct = 0.0
        holding_days = 0
        if trade.entry_price > 0:
            pnl_pct = (trade.exit_price - trade.entry_price) / trade.entry_price * 100
        if hasattr(trade, "opened_at") and trade.opened_at:
            holding_days = (trade.closed_at - trade.opened_at).days

        outcome = "win" if pnl_pct > 1.0 else ("loss" if pnl_pct < -1.0 else "breakeven")
        memory.record_trade_to_profile(
            symbol=trade.symbol, outcome=outcome,
            pnl_pct=pnl_pct, holding_days=holding_days,
        )
        updated += 1

    return StepOutput(message=f"更新 {updated} 个股票画像", data={"updated": updated})


async def reflect_decisions(ctx: StepContext) -> StepOutput:
    """Generate reflections for settled trade decisions (resolved → reflected)."""
    from trade_compass_agent.llm.providers import ChatMessage, create_chat_client
    from trade_compass_agent.ops import curate_decisions
    from trade_compass_agent.runtime.exceptions import AgentUnavailableError

    llm_call = None
    if ctx.config.agent.require_llm:
        try:
            client = create_chat_client(ctx.config)

            def _llm_call(system_prompt: str, user_content: str) -> str:
                msgs = [
                    ChatMessage(role="system", content=system_prompt),
                    ChatMessage(role="user", content=user_content),
                ]
                return client.complete(msgs).content or ""

            llm_call = _llm_call
        except AgentUnavailableError:
            llm_call = None

    reflected_ids = await asyncio.to_thread(
        curate_decisions,
        ctx.config.data_dir,
        max_reflect=10,
        llm_call=llm_call,
        trading_costs=ctx.config.trading_costs,
    )
    if reflected_ids:
        return StepOutput(
            message=f"决策复盘: 完成 {len(reflected_ids)} 条",
            data={"reflected_count": len(reflected_ids), "reflected_ids": reflected_ids},
        )
    return StepOutput(message="决策复盘: 暂无待复盘决策", data={"reflected_count": 0, "reflected_ids": []})


async def agent_eod_reflection(ctx: StepContext) -> StepOutput:
    """Agent operation: compare signals vs actual performance."""
    pnl = ctx.upstream.get("pnl_review", StepOutput.empty())
    signals = ctx.upstream.get("signal_tracking", StepOutput.empty())
    artifacts = ctx.upstream.get("research_artifacts", StepOutput.empty())

    prompt = (
        f"今日盘后反思 {ctx.date.isoformat()}。对比信号预测与实际表现：\n\n"
        f"{_reflection_section(ctx)}"
        f"## 今日 P&L\n{json.dumps(pnl.data, ensure_ascii=False, indent=2)}\n\n"
        f"## 信号追踪\n活跃信号 {signals.data.get('active_count', 0)} 条，已更新 {signals.data.get('updated', 0)} 条\n\n"
        f"## 研究资产追踪\n{json.dumps(artifacts.data, ensure_ascii=False, indent=2)}\n\n"
        "请分析：1) 哪些决策做对了？为什么？ 2) 哪些决策有问题？如何改进？ 3) 需要调整的交易规则？"
    )
    return await run_agent_step(ctx, prompt, "eod_review", step_id="agent_eod_reflection")


# ═══════════════════════════════════════════════════════════════════════════
# postmarket steps
# ═══════════════════════════════════════════════════════════════════════════

async def write_audit_summary(ctx: StepContext) -> StepOutput:
    """Write daily audit summary to vault."""
    from trade_compass_agent.config import settings_from_config
    from trade_compass_agent.memory import MemoryVault
    from trade_compass_agent.ops.audit import JsonAuditLog

    settings = settings_from_config(ctx.config)
    vault = MemoryVault(settings.memory_dir)
    audit = JsonAuditLog(ctx.config.data_dir / "audit.jsonl")
    today = ctx.date

    today_turns = [e for e in audit.events if e.event_type == "agent_turn" and e.timestamp.date() == today]

    lines = [f"# Post-market Summary {today.isoformat()}", "", f"- audit_events: {len(audit.events)}", "", "## Agent Turns Today"]
    if today_turns:
        for event in today_turns[-10:]:
            symbols = ", ".join(event.payload.get("symbols", []))
            lines.append(f"- {event.timestamp:%H:%M} {event.summary[:80]} [{symbols}]")
    else:
        lines.append("- (no agent turns today)")

    lines.extend(["", "## Recommendations"])
    recs = [e for e in audit.events if e.event_type == "recommendation"]
    for event in recs[-15:]:
        lines.append(f"- {event.timestamp.date()} {event.summary}")
    if not recs:
        lines.append("- (none)")

    path = vault.root / "daily_reviews" / f"{today.isoformat()}-audit.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    return StepOutput(
        message=f"审计摘要: {len(today_turns)} 次 Agent 对话",
        data={"agent_turns": len(today_turns), "artifact": str(path)},
    )


async def compact_memory(ctx: StepContext) -> StepOutput:
    """Compact memory summaries using LLM."""
    from trade_compass_agent.runtime.learning import compact_memory_summary

    path = compact_memory_summary(ctx.config)
    if path:
        return StepOutput(message=f"记忆已压缩至 {path.name}", data={"artifact": str(path)})
    return StepOutput(message="无需压缩", data={})


async def curate_knowledge(ctx: StepContext) -> StepOutput:
    """Curator: archive stale/inactive entries, scan conflicts, semantic merge."""
    curator_cfg = ctx.config.memory.curator
    if not curator_cfg.enabled:
        return StepOutput(message="Curator 已禁用", data={})

    from trade_compass_agent.llm.providers import ChatMessage, create_chat_client
    from trade_compass_agent.memory.contradiction import apply_conflict_reports, scan_active_conflicts
    from trade_compass_agent.memory.memory_store import MemoryStore
    from trade_compass_agent.memory.semantic_merge import merge_similar_entries
    from trade_compass_agent.memory.skill_store import SkillStore
    from trade_compass_agent.memory.write_gate import SemanticWriteGate
    from trade_compass_agent.runtime.bootstrap import GROUNDING_RULES

    skill_store = SkillStore(ctx.config.memory_dir / "skills")
    gate = SemanticWriteGate(skill_store=skill_store)
    gov = ctx.config.memory.governance
    mem_store = MemoryStore(
        ctx.config.memory_dir,
        write_gate=gate,
        min_inject_confidence=gov.min_inject_confidence,
    )

    def _llm_call(system_prompt: str, user_content: str) -> str:
        client = create_chat_client(ctx.config)
        msgs = [ChatMessage(role="system", content=system_prompt), ChatMessage(role="user", content=user_content)]
        return client.complete(msgs).content or ""

    archived_confidence = mem_store.archive_stale("memory")
    archived_inactive = mem_store.archive_inactive("memory", curator_cfg.stale_days)

    conflicts_applied: list[dict[str, str]] = []
    if curator_cfg.scan_conflicts and mem_store.list_active("memory", min_confidence=gov.min_inject_confidence):
        skills = skill_store.list_skills(include_stale=False)
        skills_summary = "\n".join(f"- {s.name}: {s.description or ''}" for s in skills[:20]) if skills else ""
        active = mem_store.list_active("memory", min_confidence=gov.min_inject_confidence)
        reports = scan_active_conflicts(active, GROUNDING_RULES, skills_summary, _llm_call)
        conflicts_applied = apply_conflict_reports(reports, mem_store)

    merged_clusters = 0
    try:
        merged_clusters = merge_similar_entries(mem_store, _llm_call)
    except Exception as exc:
        logger.warning("Semantic merge in curator failed: %s", exc)

    return StepOutput(
        message=(
            f"Curator: {len(conflicts_applied)} conflict fix(es), "
            f"{len(archived_confidence)} low-confidence, "
            f"{len(archived_inactive)} inactive, {merged_clusters} merged"
        ),
        data={
            "conflicts_applied": len(conflicts_applied),
            "archived_confidence": len(archived_confidence),
            "archived_inactive": len(archived_inactive),
            "merged_clusters": merged_clusters,
            "actions": conflicts_applied,
        },
    )


async def resolve_reflections(ctx: StepContext) -> StepOutput:
    """Resolve pending reflections from all built-in jobs."""
    from trade_compass_agent.ops.reflection_resolver import resolve_all_job_reflections

    results = resolve_all_job_reflections(ctx.config.memory_dir, ctx.config)
    total = sum(len(v) for v in results.values())
    if total:
        lessons = [r.lesson for resolved in results.values() for r in resolved]
        return StepOutput(
            message=f"反思决议: 处理了 {total} 条 pending",
            data={
                "resolved_count": total,
                "by_job": {job_id: len(resolved) for job_id, resolved in results.items()},
                "lessons": lessons,
            },
        )
    return StepOutput(message="反思决议: 暂无 pending", data={})


async def update_research_artifacts(ctx: StepContext) -> StepOutput:
    """Summarize durable catalyst and idea workflow artifacts."""
    return await update_artifact_tracking(ctx)


async def agent_daily_journal(ctx: StepContext) -> StepOutput:
    """Agent operation: write a reflective daily trading journal."""
    audit = ctx.upstream.get("audit_summary", StepOutput.empty())
    reflection = ctx.upstream.get("reflection", StepOutput.empty())
    memory = ctx.upstream.get("memory_compact", StepOutput.empty())

    today_lessons = reflection.data.get("lessons") or []
    prompt = (
        f"今日交易日记 {ctx.date.isoformat()}。请撰写一份有逻辑、有反思的交易日记：\n\n"
        f"## 今日数据\n"
        f"- Agent 对话次数: {audit.data.get('agent_turns', 0)}\n"
        f"- 记忆压缩: {memory.message}\n"
        f"- 反思决议: {reflection.message}\n\n"
        f"{_reflection_section(ctx)}"
        + (
            f"## 今日新决议\n{json.dumps(today_lessons, ensure_ascii=False)}\n\n"
            if today_lessons
            else ""
        )
        + "请写一份简洁的交易日记（200-400字），重点反思今日的决策质量和学到的教训。"
    )
    return await run_agent_step(ctx, prompt, "postmarket", step_id="agent_daily_journal")


# ═══════════════════════════════════════════════════════════════════════════
# weekly steps
# ═══════════════════════════════════════════════════════════════════════════

async def write_weekly_summary(ctx: StepContext) -> StepOutput:
    """Write weekly summary to vault."""
    from trade_compass_agent.config import settings_from_config
    from trade_compass_agent.memory import MemoryVault
    from trade_compass_agent.ops.audit import JsonAuditLog

    settings = settings_from_config(ctx.config)
    vault = MemoryVault(settings.memory_dir)
    audit = JsonAuditLog(ctx.config.data_dir / "audit.jsonl")

    week_turns = [e for e in audit.events if e.event_type == "agent_turn" and (ctx.date - e.timestamp.date()).days < 7]
    symbols: set[str] = set()
    for event in week_turns:
        for sym in event.payload.get("symbols", []):
            symbols.add(sym)

    recs = [e for e in audit.events if e.event_type == "recommendation"]

    lines = [
        f"# Weekly Summary {ctx.date.isoformat()}", "",
        f"- audit_events: {len(audit.events)}",
        f"- daily_reviews: {len(list((vault.root / 'daily_reviews').glob('*.md')))}",
        "", "## Agent Activity This Week",
    ]
    if week_turns:
        lines.append(f"- {len(week_turns)} turns, symbols: {', '.join(sorted(symbols)[:10])}")
    else:
        lines.append("- (no agent turns this week)")
    lines.extend(["", "## Recommendations"])
    for event in recs[-20:]:
        lines.append(f"- {event.timestamp.date()} {event.summary}")
    if not recs:
        lines.append("- (none)")

    path = vault.root / "weekly_summaries" / f"{ctx.date.isoformat()}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    return StepOutput(
        message=f"周度摘要: {len(week_turns)} 次对话, {len(symbols)} 只标的",
        data={"turns": len(week_turns), "symbols": sorted(symbols), "artifact": str(path)},
    )


async def agent_strategy_review(ctx: StepContext) -> StepOutput:
    """Agent operation: weekly strategy review."""
    summary = ctx.upstream.get("weekly_summary", StepOutput.empty())
    artifacts = ctx.upstream.get("weekly_research_artifacts", StepOutput.empty())

    prompt = (
        f"本周策略回顾 {ctx.date.isoformat()}：\n\n"
        f"{_reflection_section(ctx)}"
        f"## 本周摘要\n{json.dumps(summary.data, ensure_ascii=False, indent=2)}\n\n"
        f"## 研究资产追踪\n{json.dumps(artifacts.data, ensure_ascii=False, indent=2)}\n\n"
        "请深度分析：1) 本周胜率/盈亏比趋势 2) 行业暴露分析 3) 当前策略的主要风险 4) 下周策略调整建议"
    )
    return await run_agent_step(ctx, prompt, "weekly", step_id="agent_strategy_review")


# ═══════════════════════════════════════════════════════════════════════════
# dreaming steps (postmarket + weekly)
# ═══════════════════════════════════════════════════════════════════════════

async def run_dreaming(ctx: StepContext) -> StepOutput:
    """Post-market Dreaming: full memory consolidation pipeline."""
    from trade_compass_agent.memory.decision_store import DecisionStore
    from trade_compass_agent.memory.dream_diary import (
        append_dream_diary,
        build_dreaming_summary,
        run_dream_diary,
        run_procedure_extraction,
    )
    from trade_compass_agent.memory.insights import generate_insights, persist_insights
    from trade_compass_agent.memory.memory_store import MemoryStore
    from trade_compass_agent.memory.observation_store import ObservationStore
    from trade_compass_agent.memory.patterns import discover_patterns, persist_patterns
    from trade_compass_agent.memory.promotion import apply_promotions, rank_promotion_candidates
    from trade_compass_agent.memory.session_summary_store import SessionSummaryStore
    from trade_compass_agent.memory.time_tree import TimeTree
    from trade_compass_agent.memory.write_gate import SemanticWriteGate
    from trade_compass_agent.memory.skill_store import SkillStore
    from trade_compass_agent.llm.providers import ChatMessage, create_chat_client

    obs_store = ObservationStore(ctx.config.data_dir / "observations.db")
    dec_store = DecisionStore(ctx.config.data_dir)
    session_store = SessionSummaryStore(ctx.config.data_dir / "sessions.db")
    skill_store = SkillStore(ctx.config.memory_dir / "skills")
    gate = SemanticWriteGate(skill_store=skill_store)
    mem_store = MemoryStore(
        ctx.config.memory_dir,
        write_gate=gate,
        min_inject_confidence=ctx.config.memory.governance.min_inject_confidence,
    )
    time_tree = TimeTree(ctx.config.data_dir / "time_tree.db")
    today = ctx.date.isoformat()

    def _llm_call(system_prompt: str, user_content: str) -> str:
        client = create_chat_client(ctx.config)
        msgs = [ChatMessage(role="system", content=system_prompt), ChatMessage(role="user", content=user_content)]
        return client.complete(msgs).content or ""

    # Phase 6: seal today's time node
    obs_recent = obs_store.recent(limit=100, session_id=None)
    today_obs = [o for o in obs_recent if o.created_at[:10] == today]
    sessions = session_store.recent(limit=20)
    today_sessions = [s for s in sessions if (s.started_at or "")[:10] == today]

    day_node = time_tree.seal_day(
        date=today,
        obs_summaries=[o.summary for o in today_obs],
        session_summaries=[s.summary for s in today_sessions],
        concepts=[c for o in today_obs for c in o.concepts],
        symbols=[c for o in today_obs for c in o.concepts if len(c) == 6 and c[0] in "036"],
        obs_ids=[o.id for o in today_obs],
        session_ids=[s.session_id for s in today_sessions],
        llm_call=_llm_call,
    )
    time_tree.maybe_cascade(today, llm_call=_llm_call)

    # Phase 2: pattern discovery
    patterns = discover_patterns(obs_store, dec_store, _llm_call, lookback_days=3, memory_dir=ctx.config.memory_dir)
    if patterns:
        persist_patterns(ctx.config.memory_dir, patterns)
        daily_ids = {oid for p in patterns for oid in p.evidence}
        if daily_ids:
            obs_store.bump_daily(list(daily_ids))

    # Phase 3: insights
    insights = generate_insights(obs_store, dec_store, patterns, memory_dir=ctx.config.memory_dir)
    if insights:
        persist_insights(ctx.config.memory_dir, insights)
        grounded_ids = {eid for i in insights for eid in i.evidence}
        if grounded_ids:
            obs_store.bump_grounded(list(grounded_ids))

    # Phase 1: promotion scoring (after signal bumping) — four-gate pipeline
    candidates = rank_promotion_candidates(obs_store, skill_store=skill_store)
    promoted = apply_promotions(
        candidates, mem_store, obs_store,
        max_promote=5, llm_call=_llm_call, skill_store=skill_store,
        governance=ctx.config.memory.governance,
        promotion_config=ctx.config.memory.promotion,
        promoted_by_run_id=ctx.run_id or f"dreaming-{today}",
        promoted_by_job_id=ctx.job_id,
    )

    # Self-heal: if injectable KNOWLEDGE is empty and we have enough observations, bootstrap.
    # Low-trust drafts and archived rows remain on disk but should not block bootstrap.
    active_knowledge = mem_store.list_active("memory", min_confidence=ctx.config.memory.governance.min_inject_confidence)
    if not promoted and not active_knowledge and obs_store.count() >= 20:
        from trade_compass_agent.memory.promotion import BOOTSTRAP_THRESHOLD
        logger.info("[SELF-HEAL] KNOWLEDGE.md empty with %d observations — bootstrapping", obs_store.count())
        boot_candidates = rank_promotion_candidates(obs_store, bootstrap=True, limit=50, skill_store=skill_store)
        promoted = apply_promotions(
            boot_candidates, mem_store, obs_store,
            max_promote=3, threshold=BOOTSTRAP_THRESHOLD, llm_call=_llm_call, skill_store=skill_store,
            governance=ctx.config.memory.governance,
            promotion_config=ctx.config.memory.promotion,
            promoted_by_run_id=ctx.run_id or f"dreaming-bootstrap-{today}",
            promoted_by_job_id=ctx.job_id,
        )
        if promoted:
            logger.info("[SELF-HEAL] Bootstrap promoted %d entries", len(promoted))

    # Phase 4: procedural extraction (Agent session)
    procedures_text = ""
    strong_patterns = [p for p in patterns if p.strength >= 0.7]
    if strong_patterns:
        try:
            procedures_text = run_procedure_extraction(ctx.config, strong_patterns)
        except Exception as exc:
            logger.warning("Procedural extraction failed: %s", exc)

    # Phase 5: dream diary (Agent session)
    dreaming_summary = build_dreaming_summary(day_node, patterns, promoted, insights, procedures_text, memory_dir=ctx.config.memory_dir)
    try:
        diary_entry = run_dream_diary(ctx.config, dreaming_summary)
        append_dream_diary(ctx.config.memory_dir, diary_entry)
    except Exception as exc:
        logger.warning("Dream diary generation failed: %s", exc)
        diary_entry = ""

    # Phase 7: semantic merge handled by curate_knowledge step (postmarket job)

    return StepOutput(
        message=f"Dreaming: {len(patterns)} patterns, {len(promoted)} promoted, "
                f"{len(insights)} insights",
        data={
            "patterns": len(patterns),
            "promoted": len(promoted),
            "insights": len(insights),
            "procedures": bool(procedures_text),
            "diary": bool(diary_entry),
        },
    )


async def run_weekly_dreaming(ctx: StepContext) -> StepOutput:
    """Weekly deep dreaming: week seal + longer lookback + deep diary."""
    from trade_compass_agent.memory.decision_store import DecisionStore
    from trade_compass_agent.memory.dream_diary import (
        append_dream_diary,
        build_dreaming_summary,
        run_dream_diary,
        run_procedure_extraction,
    )
    from trade_compass_agent.memory.insights import generate_insights, persist_insights
    from trade_compass_agent.memory.memory_store import MemoryStore
    from trade_compass_agent.memory.observation_store import ObservationStore
    from trade_compass_agent.memory.patterns import discover_patterns, persist_patterns
    from trade_compass_agent.memory.promotion import apply_promotions, rank_promotion_candidates
    from trade_compass_agent.memory.time_tree import TimeTree, current_week_id
    from trade_compass_agent.memory.write_gate import SemanticWriteGate
    from trade_compass_agent.memory.skill_store import SkillStore
    from trade_compass_agent.llm.providers import ChatMessage, create_chat_client

    obs_store = ObservationStore(ctx.config.data_dir / "observations.db")
    dec_store = DecisionStore(ctx.config.data_dir)
    skill_store = SkillStore(ctx.config.memory_dir / "skills")
    gate = SemanticWriteGate(skill_store=skill_store)
    mem_store = MemoryStore(
        ctx.config.memory_dir,
        write_gate=gate,
        min_inject_confidence=ctx.config.memory.governance.min_inject_confidence,
    )
    time_tree = TimeTree(ctx.config.data_dir / "time_tree.db")
    today = ctx.date.isoformat()

    def _llm_call(system_prompt: str, user_content: str) -> str:
        client = create_chat_client(ctx.config)
        msgs = [ChatMessage(role="system", content=system_prompt), ChatMessage(role="user", content=user_content)]
        return client.complete(msgs).content or ""

    # Seal week if not already done
    week_id = current_week_id()
    existing = time_tree.get_node(week_id)
    if not existing or not existing.sealed_at:
        day_nodes = time_tree.nodes_by_level("day", sealed_only=True)
        cutoff = (ctx.date - timedelta(days=7)).isoformat()
        this_week_days = [n for n in day_nodes if n.id >= cutoff]
        if this_week_days:
            time_tree.seal_week(week_id, this_week_days, _llm_call)

    # 7-day lookback patterns
    patterns = discover_patterns(obs_store, dec_store, _llm_call, lookback_days=7, memory_dir=ctx.config.memory_dir)
    if patterns:
        persist_patterns(ctx.config.memory_dir, patterns)
        daily_ids = {oid for p in patterns for oid in p.evidence}
        if daily_ids:
            obs_store.bump_daily(list(daily_ids))

    # Insights
    insights = generate_insights(obs_store, dec_store, patterns, memory_dir=ctx.config.memory_dir)
    if insights:
        persist_insights(ctx.config.memory_dir, insights)
        grounded_ids = {eid for i in insights for eid in i.evidence}
        if grounded_ids:
            obs_store.bump_grounded(list(grounded_ids))

    # Promotion (after signal bumping) — four-gate pipeline
    candidates = rank_promotion_candidates(obs_store, skill_store=skill_store)
    promoted = apply_promotions(
        candidates, mem_store, obs_store,
        max_promote=5, llm_call=_llm_call, skill_store=skill_store,
        governance=ctx.config.memory.governance,
        promotion_config=ctx.config.memory.promotion,
        promoted_by_run_id=ctx.run_id or f"dreaming-{today}",
        promoted_by_job_id=ctx.job_id,
    )

    # Self-heal: bootstrap if injectable KNOWLEDGE is empty.
    active_knowledge = mem_store.list_active("memory", min_confidence=ctx.config.memory.governance.min_inject_confidence)
    if not promoted and not active_knowledge and obs_store.count() >= 20:
        from trade_compass_agent.memory.promotion import BOOTSTRAP_THRESHOLD
        logger.info("[SELF-HEAL] Weekly: KNOWLEDGE.md empty — bootstrapping")
        boot_candidates = rank_promotion_candidates(obs_store, bootstrap=True, limit=50, skill_store=skill_store)
        promoted = apply_promotions(
            boot_candidates, mem_store, obs_store,
            max_promote=3, threshold=BOOTSTRAP_THRESHOLD, llm_call=_llm_call, skill_store=skill_store,
            governance=ctx.config.memory.governance,
            promotion_config=ctx.config.memory.promotion,
            promoted_by_run_id=ctx.run_id or f"dreaming-bootstrap-{today}",
            promoted_by_job_id=ctx.job_id,
        )
        if promoted:
            logger.info("[SELF-HEAL] Bootstrap promoted %d entries", len(promoted))

    # Procedural extraction
    procedures_text = ""
    strong_patterns = [p for p in patterns if p.strength >= 0.7]
    if strong_patterns:
        try:
            procedures_text = run_procedure_extraction(ctx.config, strong_patterns)
        except Exception as exc:
            logger.warning("Weekly procedural extraction failed: %s", exc)

    # Weekly dream diary
    week_node = time_tree.get_node(week_id)
    dreaming_summary = build_dreaming_summary(week_node, patterns, promoted, insights, procedures_text, memory_dir=ctx.config.memory_dir)
    try:
        diary_entry = run_dream_diary(ctx.config, dreaming_summary, weekly=True)
        append_dream_diary(ctx.config.memory_dir, diary_entry)
    except Exception as exc:
        logger.warning("Weekly dream diary failed: %s", exc)

    return StepOutput(
        message=f"Weekly Dreaming: {len(patterns)} patterns, {len(promoted)} promoted, {len(insights)} insights",
        data={
            "patterns": len(patterns),
            "promoted": len(promoted),
            "insights": len(insights),
        },
    )


# ═══════════════════════════════════════════════════════════════════════════
# Shared helpers
# ═══════════════════════════════════════════════════════════════════════════

def _load_portfolio_mtm(config):
    """Load paper portfolio with fresh market prices."""
    from trade_compass_agent.portfolio import JsonPaperPortfolio
    from trade_compass_agent.runtime.market_stack import MarketStack

    portfolio = JsonPaperPortfolio(
        config.data_dir / "paper_trades.jsonl",
        costs=config.trading_costs,
    )
    stack = MarketStack.from_config(config)
    return portfolio, portfolio.positions_with_market_prices(stack.provider)


def _position_pnl_pct(position) -> float:
    if position.avg_cost > 0:
        return (position.last_price / position.avg_cost - 1) * 100
    return 0.0


def _reflection_section(ctx: StepContext) -> str:
    """Format resolved reflection context for Agent prompts."""
    if not ctx.reflection_context:
        return ""
    return f"## 历史反思\n{ctx.reflection_context}\n\n"


def _load_signal_map(config) -> dict:
    path = config.data_dir / "signals.jsonl"
    if not path.exists():
        return {}
    result = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                sig = json.loads(line)
                result[sig["symbol"]] = sig
            except (json.JSONDecodeError, KeyError):
                continue
    return result


def _exit_alerts_for_position(position, signal_map: dict) -> list[str]:
    from trade_compass_agent.portfolio.lot_sizing import format_pnl_alert

    alerts: list[str] = []
    sig = signal_map.get(position.symbol)
    if sig and sig.get("stop_loss") and position.last_price <= sig["stop_loss"]:
        alerts.append(f"{position.symbol} 触发止损(当前{position.last_price}, 止损{sig['stop_loss']})")
    if position.avg_cost > 0:
        pnl_pct = (position.last_price / position.avg_cost - 1) * 100
        lot_alert = format_pnl_alert(position.symbol, position.quantity, pnl_pct)
        if lot_alert:
            alerts.append(lot_alert)
    return alerts
