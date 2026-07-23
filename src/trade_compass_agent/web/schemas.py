from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class _AttrBase(BaseModel):
    """Base for payloads built from dataclasses via `model_validate(...)`.

    `from_attributes=True` lets Pydantic v2 read attributes off plain or frozen
    dataclasses. Enum fields are mapped to `str` so StrEnum values serialize as
    their value strings on the wire.
    """

    model_config = ConfigDict(from_attributes=True)


# --- Primitive payloads -----------------------------------------------------


class BarPayload(_AttrBase):
    symbol: str
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    amount: float | None = None
    adjusted: bool = False


class SectorStrengthPayload(_AttrBase):
    name: str
    change_pct: float
    turnover_pct: float | None = None
    up_count: int | None = None
    down_count: int | None = None
    leader: str | None = None
    leader_change_pct: float | None = None


class LimitUpSummaryPayload(_AttrBase):
    count: int
    strong_count: int
    top_industries: list[str]
    leaders: list[str]


class MarketPulseResponse(_AttrBase):
    timestamp: datetime
    provider_name: str
    sectors: list[SectorStrengthPayload]
    limit_up: LimitUpSummaryPayload
    notes: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class EventPayload(_AttrBase):
    symbol: str | None
    event_type: str
    title: str
    timestamp: datetime
    source: str
    payload: dict[str, Any] = Field(default_factory=dict)


class FundamentalsPayload(_AttrBase):
    symbol: str
    pe_ttm: float | None = None
    pb: float | None = None
    market_cap: float | None = None
    roe: float | None = None
    provider_name: str = "unknown"
    notes: list[str] = Field(default_factory=list)


class EventsResponse(BaseModel):
    symbol: str
    events: list[EventPayload]
    provider_name: str | None = None


# --- Bars -------------------------------------------------------------------


class BarsResponse(BaseModel):
    symbol: str
    timeframe: str
    limit: int
    bars: list[BarPayload]
    quality_warnings: list[str] = Field(default_factory=list)
    provider_name: str | None = None


# --- Portfolio --------------------------------------------------------------


class PaperTradePayload(_AttrBase):
    trade_id: str = ""
    decision_id: str | None = None
    symbol: str
    account: str
    side: str
    quantity: int
    price: float
    timestamp: datetime
    reason: str
    price_source: str = "provided_execution"
    price_as_of: datetime | None = None
    requested_price: float | None = None
    previous_close: float | None = None
    suspended: bool = False
    is_st: bool = False


class PaperTradeCreate(BaseModel):
    symbol: str
    account: str
    side: Literal["buy", "sell"]
    quantity: int
    price: float
    reason: str = "manual paper trade"
    previous_close: float | None = None
    suspended: bool = False
    is_st: bool = False


class AccountCreate(BaseModel):
    kind: str
    name: str
    description: str | None = None
    capital: float = 0.0


class AccountUpdate(BaseModel):
    kind: str | None = None
    name: str | None = None
    description: str | None = None
    capital: float | None = None


class PortfolioPositionPayload(_AttrBase):
    symbol: str
    account: str
    quantity: int
    avg_cost: float
    last_price: float
    market_value: float = 0.0
    unrealized_pnl: float = 0.0
    name: str = ""
    opened_at: datetime | None = None


class AccountSummaryPayload(_AttrBase):
    account: str
    position_count: int
    market_value: float
    cost_basis: float
    unrealized_pnl: float
    realized_pnl: float = 0.0
    fees: float = 0.0
    wins: int = 0
    losses: int = 0
    win_rate: float = 0.0
    payoff_ratio: float = 0.0
    max_drawdown: float = 0.0


class RealizedTradePayload(_AttrBase):
    account: str
    symbol: str
    quantity: int
    entry_price: float
    exit_price: float
    pnl: float
    fees: float
    opened_at: datetime
    closed_at: datetime


class TradingCostsPayload(_AttrBase):
    commission_rate: float
    min_commission: float
    stamp_duty_rate: float
    transfer_fee_rate: float
    slippage_bps: float
    min_lot_size: int
    price_limit_pct: float
    st_price_limit_pct: float


class PortfolioResponse(BaseModel):
    accounts: list[AccountSummaryPayload]
    positions_by_account: dict[str, list[PortfolioPositionPayload]]
    trades: list[PaperTradePayload]
    realized_trades: list[RealizedTradePayload]
    costs: TradingCostsPayload


# --- Audit / Review ---------------------------------------------------------


class AuditEventPayload(_AttrBase):
    id: str
    timestamp: datetime
    event_type: str
    summary: str
    payload: dict[str, Any] = Field(default_factory=dict)


# --- User rules -------------------------------------------------------------


class RuleEntryPayload(_AttrBase):
    id: str
    text: str
    enabled: bool = True
    updated_by: str = "user"
    created_at: str = ""
    updated_at: str = ""
    content_hash: str = ""


class RulesResponse(BaseModel):
    content: str
    entries: list[RuleEntryPayload]
    chars_used: int
    limit: int
    version: str


class RulesReplace(BaseModel):
    content: str


class RuleEntryCreate(BaseModel):
    text: str


class RuleEntryUpdate(BaseModel):
    text: str


class RulePerformanceRowPayload(_AttrBase):
    rule_id: str
    title: str
    layer: str | None = None
    signal_count: int
    avg_return_1d: float | None = None
    avg_return_3d: float | None = None
    win_rate_1d: float | None = None
    experimental: bool = False


class RulePerformanceReportPayload(BaseModel):
    rows: list[RulePerformanceRowPayload]
    warnings: list[str] = Field(default_factory=list)


# --- Skills & Memory ---------------------------------------------------------


class SkillPayload(BaseModel):
    name: str
    description: str
    category: str
    state: str = "active"
    pinned: bool = False
    use_count: int = 0
    patch_count: int = 0
    last_used_at: str | None = None
    created_at: str | None = None
    created_by: str | None = None


class SkillDetailPayload(SkillPayload):
    content: str


class SkillsResponse(BaseModel):
    skills: list[SkillPayload]
    total: int


class SkillPinRequest(BaseModel):
    pinned: bool


class MemoryEntryPayload(BaseModel):
    index: int
    text: str
    confidence: float = 1.0
    access_count: int = 0
    source: str = "agent"
    status: str = "active"
    content_hash: str = ""
    created_at: str = ""
    last_accessed: str | None = None


class MemoryResponse(BaseModel):
    target: str
    entries: list[MemoryEntryPayload]
    chars_used: int
    char_limit: int


class MemoryActionRequest(BaseModel):
    content: str


# --- Scheduling / Jobs / Notifications --------------------------------------


class ScheduledJobPayload(_AttrBase):
    id: str
    name: str
    cadence: str
    enabled: bool
    workflow_id: str | None = None
    delivery_channels: list[str] = []


class JobRunPayload(_AttrBase):
    id: str
    job_id: str
    status: str
    started_at: datetime
    finished_at: datetime | None = None
    ok: bool
    message: str
    artifact: str | None = None
    error: str | None = None


class NotificationPayload(_AttrBase):
    channel: str
    title: str
    message: str
    severity: str = "info"


class SchedulerConfigPayload(_AttrBase):
    enabled: bool
    timezone: str
    premarket_time: str
    morning_plan_time: str
    close_time: str
    eod_review_time: str
    postmarket_time: str
    weekly_day: str
    weekly_time: str


class SchedulerConfigUpdateRequest(BaseModel):
    enabled: bool | None = None
    timezone: str | None = None
    premarket_time: str | None = None
    morning_plan_time: str | None = None
    close_time: str | None = None
    eod_review_time: str | None = None
    postmarket_time: str | None = None
    weekly_day: str | None = None
    weekly_time: str | None = None


class SchedulerConfigUpdateResponse(BaseModel):
    config: SchedulerConfigPayload
    reloaded: bool
    message: str


# --- Config -----------------------------------------------------------------


class WatchlistsResponse(_AttrBase):
    stocks: list[str]
    etfs: list[str]
    mid_term: list[str]


# --- Generic ----------------------------------------------------------------


class ErrorResponse(BaseModel):
    error: str
