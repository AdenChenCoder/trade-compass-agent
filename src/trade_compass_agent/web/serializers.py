from __future__ import annotations

from collections import defaultdict

from trade_compass_agent.data.fundamentals import FundamentalsSnapshot
from trade_compass_agent.config import SchedulerConfig, TradingCostConfig, Watchlists
from trade_compass_agent.domain import (
    AccountKind,
    AuditEvent,
    Bar,
    Event,
    LimitUpSummary,
    MarketPulse,
    Notification,
    PaperTrade,
    PortfolioPosition,
    SectorStrength,
)
from trade_compass_agent.evaluation.rule_performance import RulePerformanceReport, RulePerformanceRow
from trade_compass_agent.memory.rules_store import RuleEntry
from trade_compass_agent.ops.run_store import RunRecord
from trade_compass_agent.portfolio.simulator import AccountSummary, JsonPaperPortfolio, RealizedTrade

from . import schemas as s


def to_bar_payload(bar: Bar) -> s.BarPayload:
    return s.BarPayload.model_validate(bar)


def to_sector_payload(sector: SectorStrength) -> s.SectorStrengthPayload:
    return s.SectorStrengthPayload.model_validate(sector)


def to_limit_up_payload(summary: LimitUpSummary) -> s.LimitUpSummaryPayload:
    return s.LimitUpSummaryPayload.model_validate(summary)


def to_pulse_payload(pulse: MarketPulse | None) -> s.MarketPulseResponse | None:
    if pulse is None:
        return None
    return s.MarketPulseResponse(
        timestamp=pulse.timestamp,
        provider_name=pulse.provider_name,
        sectors=[to_sector_payload(item) for item in pulse.sectors],
        limit_up=to_limit_up_payload(pulse.limit_up),
        notes=list(pulse.notes),
        warnings=list(pulse.warnings),
    )


def to_event_payload(event: Event) -> s.EventPayload:
    return s.EventPayload.model_validate(event)


def to_fundamentals_payload(snapshot: FundamentalsSnapshot | None) -> s.FundamentalsPayload | None:
    if snapshot is None:
        return None
    return s.FundamentalsPayload(
        symbol=snapshot.symbol,
        pe_ttm=snapshot.pe_ttm,
        pb=snapshot.pb,
        market_cap=snapshot.market_cap,
        roe=snapshot.roe,
        provider_name=snapshot.provider_name,
        notes=list(snapshot.notes),
    )


def to_audit_payload(event: AuditEvent) -> s.AuditEventPayload:
    return s.AuditEventPayload.model_validate(event)


def to_paper_trade_payload(trade: PaperTrade) -> s.PaperTradePayload:
    return s.PaperTradePayload(
        trade_id=trade.trade_id,
        decision_id=trade.decision_id,
        symbol=trade.symbol,
        account=trade.account.value,
        side=trade.side,
        quantity=trade.quantity,
        price=trade.price,
        timestamp=trade.timestamp,
        reason=trade.reason,
        price_source=trade.price_source,
        price_as_of=trade.price_as_of,
        requested_price=trade.requested_price,
        previous_close=trade.previous_close,
        suspended=trade.suspended,
        is_st=trade.is_st,
    )


def to_position_payload(position: PortfolioPosition) -> s.PortfolioPositionPayload:
    return s.PortfolioPositionPayload(
        symbol=position.symbol,
        account=position.account.value,
        quantity=position.quantity,
        avg_cost=position.avg_cost,
        last_price=position.last_price,
        market_value=position.market_value,
        unrealized_pnl=position.unrealized_pnl,
        name=position.name,
        opened_at=position.opened_at,
    )


def to_account_summary_payload(summary: AccountSummary) -> s.AccountSummaryPayload:
    return s.AccountSummaryPayload(
        account=summary.account.value,
        position_count=summary.position_count,
        market_value=summary.market_value,
        cost_basis=summary.cost_basis,
        unrealized_pnl=summary.unrealized_pnl,
        realized_pnl=summary.realized_pnl,
        fees=summary.fees,
        wins=summary.wins,
        losses=summary.losses,
        win_rate=summary.win_rate,
        payoff_ratio=summary.payoff_ratio,
        max_drawdown=summary.max_drawdown,
    )


def to_realized_trade_payload(trade: RealizedTrade) -> s.RealizedTradePayload:
    return s.RealizedTradePayload(
        account=trade.account.value,
        symbol=trade.symbol,
        quantity=trade.quantity,
        entry_price=trade.entry_price,
        exit_price=trade.exit_price,
        pnl=trade.pnl,
        fees=trade.fees,
        opened_at=trade.opened_at,
        closed_at=trade.closed_at,
    )


def to_trading_costs_payload(costs: TradingCostConfig) -> s.TradingCostsPayload:
    return s.TradingCostsPayload.model_validate(costs)


def to_portfolio_response(
    portfolio: JsonPaperPortfolio,
    *,
    costs: TradingCostConfig,
    live_positions: list | None = None,
) -> s.PortfolioResponse:
    portfolio.resolve_names()
    positions_grouped: dict[str, list[s.PortfolioPositionPayload]] = defaultdict(list)
    if live_positions:
        for p in live_positions:
            positions_grouped[p.account.value].append(to_position_payload(p))
    else:
        for account_kind, positions in portfolio.positions_by_account().items():
            positions_grouped[account_kind.value] = [to_position_payload(p) for p in positions]
    for account in AccountKind:
        positions_grouped.setdefault(account.value, [])
    return s.PortfolioResponse(
        accounts=[to_account_summary_payload(item) for item in portfolio.account_summaries()],
        positions_by_account=dict(positions_grouped),
        trades=[to_paper_trade_payload(item) for item in portfolio.trades],
        realized_trades=[to_realized_trade_payload(item) for item in portfolio.realized_trades()],
        costs=to_trading_costs_payload(costs),
    )


def to_rule_entry_payload(entry: RuleEntry) -> s.RuleEntryPayload:
    return s.RuleEntryPayload.model_validate(entry)


def to_rule_performance_row(row: RulePerformanceRow) -> s.RulePerformanceRowPayload:
    return s.RulePerformanceRowPayload.model_validate(row)


def to_rule_performance_payload(report: RulePerformanceReport) -> s.RulePerformanceReportPayload:
    return s.RulePerformanceReportPayload(
        rows=[to_rule_performance_row(item) for item in report.rows],
        warnings=list(report.warnings),
    )


def to_job_run_payload_v2(run: RunRecord) -> s.JobRunPayload:
    return s.JobRunPayload(
        id=run.id,
        job_id=run.job_id,
        status=run.status,
        started_at=run.started_at or run.created_at,
        finished_at=run.finished_at,
        ok=run.ok,
        message=run.message,
        artifact=run.artifact,
        error=run.error,
    )


def to_notification_payload(notification: Notification) -> s.NotificationPayload:
    return s.NotificationPayload.model_validate(notification)


def to_scheduler_config_payload(config: SchedulerConfig) -> s.SchedulerConfigPayload:
    return s.SchedulerConfigPayload.model_validate(config)


def to_watchlists_payload(watchlists: Watchlists) -> s.WatchlistsResponse:
    return s.WatchlistsResponse(
        stocks=list(watchlists.stocks),
        etfs=list(watchlists.etfs),
        mid_term=list(watchlists.mid_term),
    )
