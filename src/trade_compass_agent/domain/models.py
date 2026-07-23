from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum
from typing import Any, Literal


class InstrumentKind(StrEnum):
    STOCK = "stock"
    ETF = "etf"
    INDEX = "index"
    SECTOR = "sector"


class SignalGrade(StrEnum):
    OBSERVE = "observe"
    WAIT_PULLBACK = "wait_pullback"
    BREAKOUT_CONFIRMED = "breakout_confirmed"
    DIP_CANDIDATE = "dip_candidate"
    REJECT = "reject"
    EXIT_TRIGGER = "exit_trigger"


class TimeHorizon(StrEnum):
    SHORT_STOCK = "short_stock"
    SHORT_ETF_SECTOR = "short_etf_sector"
    MID_TERM = "mid_term"


class AccountKind(StrEnum):
    SHORT_STOCK = "short_stock"
    ETF_ROTATION = "etf_rotation"
    MID_TERM = "mid_term"
    LONG_TERM = "long_term"
    MIXED = "mixed"


@dataclass(frozen=True)
class Instrument:
    symbol: str
    name: str
    kind: InstrumentKind
    exchange: str | None = None
    sector: str | None = None


@dataclass(frozen=True)
class Bar:
    symbol: str
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    amount: float | None = None
    adjusted: bool = False
    turnover_pct: float | None = None


@dataclass(frozen=True)
class Event:
    symbol: str | None
    event_type: str
    title: str
    timestamp: datetime
    source: str
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Signal:
    symbol: str
    grade: SignalGrade
    horizon: TimeHorizon
    confidence: float
    evidence: list[str]
    risks: list[str]
    trigger: str
    invalidation: str
    source_rules: list[str] = field(default_factory=list)
    is_experimental: bool = False


@dataclass(frozen=True)
class Recommendation:
    symbol: str
    action: SignalGrade
    horizon: TimeHorizon
    position_limit_pct: float
    stop_loss: float | None
    take_profit: float | None
    rationale: str
    evidence: list[str]
    risks: list[str]
    invalidation: str
    audit_id: str | None = None


@dataclass(frozen=True)
class SectorStrength:
    name: str
    change_pct: float
    turnover_pct: float | None = None
    up_count: int | None = None
    down_count: int | None = None
    leader: str | None = None
    leader_change_pct: float | None = None


@dataclass(frozen=True)
class LimitUpSummary:
    count: int
    strong_count: int
    top_industries: list[str]
    leaders: list[str]


@dataclass(frozen=True)
class MarketPulse:
    timestamp: datetime
    provider_name: str
    sectors: list[SectorStrength]
    limit_up: LimitUpSummary
    notes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class EtfRotationCandidate:
    symbol: str
    score: float
    short_action: SignalGrade
    mid_term_action: str
    trend_score: float
    volume_score: float
    risk_score: float
    market_pulse_score: float
    evidence: list[str]
    risks: list[str]
    invalidation: str
    # Additive risk-factor fields (Phase G):
    liquidity_score: float = 0.0
    overheat_flag: bool = False
    ma20_deviation_pct: float = 0.0
    theme_crowding_note: str | None = None


@dataclass(frozen=True)
class PaperTrade:
    symbol: str
    account: AccountKind
    side: Literal["buy", "sell"]
    quantity: int
    price: float
    timestamp: datetime
    reason: str
    trade_id: str = ""
    decision_id: str | None = None
    price_source: str = "provided_execution"
    price_as_of: datetime | None = None
    requested_price: float | None = None
    previous_close: float | None = None
    suspended: bool = False
    is_st: bool = False
    is_t0: bool = False
    price_limit_pct: float = 0.10


@dataclass(frozen=True)
class PortfolioPosition:
    symbol: str
    account: AccountKind
    quantity: int
    avg_cost: float
    last_price: float
    market_value: float = 0.0
    unrealized_pnl: float = 0.0
    name: str = ""
    price_source: str = "unknown"
    price_is_fresh: bool = False
    opened_at: datetime | None = None


@dataclass(frozen=True)
class StructuredRuleCandidate:
    title: str
    content: str
    reason: str = ""


@dataclass(frozen=True)
class Review:
    review_date: date
    summary: str
    signal_scores: dict[str, float]
    lessons: list[str]
    rule_candidates: list[str]
    structured_rule_candidates: list[StructuredRuleCandidate] = field(default_factory=list)


@dataclass(frozen=True)
class CaseStudy:
    id: str
    title: str
    symbol: str | None
    outcome: Literal["success", "failure", "mixed"]
    summary: str
    lessons: list[str]


@dataclass(frozen=True)
class MemorySummary:
    period: Literal["daily", "weekly", "monthly"]
    title: str
    content: str
    created_at: datetime


@dataclass(frozen=True)
class EvaluationMetric:
    name: str
    value: float
    unit: str
    context: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SignalFollowThrough:
    audit_id: str
    symbol: str
    action: str
    signal_date: date
    entry_close: float
    return_1d: float | None
    return_3d: float | None
    return_5d: float | None
    max_runup: float | None
    max_drawdown: float | None
    status: str


@dataclass(frozen=True)
class AuditEvent:
    id: str
    timestamp: datetime
    event_type: str
    summary: str
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Notification:
    channel: str
    title: str
    message: str
    severity: Literal["info", "warning", "critical"] = "info"
