"""Daily long-only research: close signals, next-session open fills.

Missing/suspended bars cannot execute. Prior closes only mark holdings.
Slippage is a cost, matching the paper ledger. Terminal holdings stay open.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import pandas as pd

from trade_compass_agent.config import TradingCostConfig
from trade_compass_agent.portfolio.costs import estimate_trade_fee
from trade_compass_agent.portfolio.market_rules import infer_market_rules

ENGINE_VERSION = "daily-next-open-v1"


@dataclass
class BacktestConfig:
    initial_cash: float = 100_000.0
    position_pct: float = 0.10  # Fraction of available cash, including fees.
    max_positions: int = 5
    entry_threshold: float = 0.7
    exit_threshold: float = 0.3
    stop_loss_pct: float = 0.08
    take_profit_pct: float = 0.20
    commission_rate: float = 0.0003
    stamp_duty_rate: float = 0.0005
    slippage_pct: float = 0.0005
    min_lot: int = 100
    t_plus_one: bool = True
    min_commission: float = 5.0
    transfer_fee_rate: float = 0.00001

    def __post_init__(self) -> None:
        for key, value in vars(self).items():
            if isinstance(value, (int, float)) and not math.isfinite(value):
                raise ValueError(f"{key} must be finite")
        if self.initial_cash <= 0 or not 0 < self.position_pct <= 1:
            raise ValueError("initial_cash must be positive; position_pct must be in (0, 1]")
        if any(type(value) is not int or value < 1 for value in (self.max_positions, self.min_lot)):
            raise ValueError("max_positions and min_lot must be positive")
        if not 0 <= self.exit_threshold <= self.entry_threshold <= 1:
            raise ValueError("thresholds must satisfy 0 <= exit <= entry <= 1")
        if not 0 < self.stop_loss_pct < 1 or self.take_profit_pct <= 0:
            raise ValueError("invalid stop-loss or take-profit percentage")
        if min(self.commission_rate, self.stamp_duty_rate, self.slippage_pct,
               self.min_commission, self.transfer_fee_rate) < 0:
            raise ValueError("costs must be non-negative")

    def costs(self) -> TradingCostConfig:
        return TradingCostConfig(
            commission_rate=self.commission_rate, min_commission=self.min_commission,
            stamp_duty_rate=self.stamp_duty_rate, transfer_fee_rate=self.transfer_fee_rate,
            slippage_bps=self.slippage_pct * 10_000,
        )


@dataclass
class TradeRecord:
    symbol: str
    entry_date: str
    exit_date: str
    entry_price: float
    exit_price: float
    quantity: int
    direction: str
    pnl: float = 0.0
    pnl_pct: float = 0.0
    exit_reason: str = ""
    commission_total: float = 0.0  # All entry/exit costs; legacy field name.


@dataclass
class OpenPosition:
    symbol: str
    entry_date: str
    entry_price: float
    quantity: int
    entry_fee: float
    mark_price: float
    mark_date: str
    price_is_stale: bool


@dataclass
class BacktestResult:
    config: BacktestConfig
    start_date: str
    end_date: str
    initial_cash: float
    final_value: float
    total_return_pct: float
    trades: list[TradeRecord] = field(default_factory=list)
    daily_nav: list[float] = field(default_factory=list)
    sharpe_ratio: float = 0.0
    max_drawdown_pct: float = 0.0
    win_rate: float = 0.0
    avg_win_pct: float = 0.0
    avg_loss_pct: float = 0.0
    payoff_ratio: float = 0.0
    total_trades: int = 0
    profit_factor: float = 0.0
    cash: float = 0.0
    open_positions: list[OpenPosition] = field(default_factory=list)
    engine_version: str = ENGINE_VERSION


@dataclass
class _Position:
    symbol: str
    entry_date: str
    entry_price: float
    quantity: int
    entry_fee: float


def _daily_index(values) -> pd.DatetimeIndex:
    if len(values) and pd.api.types.is_numeric_dtype(getattr(values, "dtype", None)):
        raise ValueError("daily dates must not be numeric row numbers")
    index = pd.DatetimeIndex(pd.to_datetime(values, errors="raise"))
    if index.tz is not None:
        index = index.tz_convert("Asia/Shanghai").tz_localize(None)
    index = index.normalize()
    if index.hasnans or index.has_duplicates:
        raise ValueError("daily dates must be present and unique")
    return index


def normalize_daily_prices(frame: pd.DataFrame) -> pd.DataFrame:
    """Reject undated/ambiguous input rather than borrowing the final price."""
    if not {"open", "high", "low", "close", "volume"}.issubset(frame.columns):
        raise ValueError("daily prices require open, high, low, close, volume")
    if "date" not in frame and not isinstance(frame.index, pd.DatetimeIndex):
        raise ValueError("daily prices require a date column or DatetimeIndex")
    result = frame.copy()
    result.index = _daily_index(frame["date"] if "date" in frame else frame.index)
    return result.sort_index()


def _bar(frame: pd.DataFrame | None, day: pd.Timestamp) -> pd.Series | None:
    if frame is None or day not in frame.index:
        return None
    row = frame.loc[day]
    values = pd.to_numeric(row[["open", "high", "low", "close", "volume"]], errors="coerce")
    if not all(math.isfinite(v) and v > 0 for v in values):
        return None
    if values["low"] > min(values["open"], values["close"]) or values["high"] < max(values["open"], values["close"]):
        return None
    if bool(row.get("suspended", False)):
        return None
    return values


def _mark(pos: _Position, frame: pd.DataFrame, day: pd.Timestamp) -> OpenPosition:
    closes = pd.to_numeric(frame.loc[frame.index <= day, "close"], errors="coerce")
    closes = closes[closes.map(lambda value: math.isfinite(value) and value > 0)]
    price = float(closes.iloc[-1]) if len(closes) else pos.entry_price
    date = closes.index[-1].strftime("%Y-%m-%d") if len(closes) else pos.entry_date
    return OpenPosition(pos.symbol, pos.entry_date, pos.entry_price, pos.quantity,
                        pos.entry_fee, price, date, date != day.strftime("%Y-%m-%d"))


def _can_sell(pos: _Position, day: pd.Timestamp, cfg: BacktestConfig) -> bool:
    return not cfg.t_plus_one or infer_market_rules(pos.symbol).is_t0 or pos.entry_date < day.strftime("%Y-%m-%d")


def _close(pos: _Position, day: pd.Timestamp, price: float, cfg: BacktestConfig, reason: str) -> tuple[TradeRecord, float]:
    exit_fee = estimate_trade_fee(pos.quantity, price, "sell", cfg.costs())
    proceeds = pos.quantity * price - exit_fee
    basis = pos.quantity * pos.entry_price + pos.entry_fee
    pnl = proceeds - basis
    return TradeRecord(
        pos.symbol, pos.entry_date, day.strftime("%Y-%m-%d"), pos.entry_price, price,
        pos.quantity, "long", round(pnl, 4), round(pnl / basis * 100, 4), reason,
        round(pos.entry_fee + exit_fee, 4),
    ), proceeds


def run_backtest(signals: pd.DataFrame, prices: dict[str, pd.DataFrame], config: BacktestConfig | None = None) -> BacktestResult:
    """Use the previous close's score at the next dated session's open.

    Unavailable bars skip execution. Stops are pessimistic if both levels
    touch. Daily assumptions cannot model queues, auctions, or liquidity.
    """
    cfg = config or BacktestConfig()
    scores = signals.copy()
    scores.index = _daily_index(scores.index)
    scores = scores.sort_index().apply(pd.to_numeric, errors="raise")
    if ((scores < 0) | (scores > 1)).any().any():
        raise ValueError("signal scores must be in [0, 1] or missing")
    frames = {symbol: normalize_daily_prices(frame) for symbol, frame in prices.items()}
    cash = cfg.initial_cash
    positions: dict[str, _Position] = {}
    trades: list[TradeRecord] = []
    nav: list[float] = []
    opened: list[OpenPosition] = []
    costs = cfg.costs()
    for i, day in enumerate(scores.index):
        previous = scores.iloc[i - 1] if i else pd.Series(dtype=float)
        exited: set[str] = set()
        for symbol, pos in list(positions.items()):
            bar = _bar(frames.get(symbol), day)
            if bar is not None and _can_sell(pos, day, cfg) and previous.get(symbol, float("nan")) < cfg.exit_threshold:
                trade, proceeds = _close(pos, day, float(bar["open"]), cfg, "signal")
                trades.append(trade)
                cash += proceeds
                del positions[symbol]
                exited.add(symbol)
        for symbol, score in previous.sort_values(ascending=False).items():
            if len(positions) >= cfg.max_positions:
                break
            if pd.isna(score) or score <= cfg.entry_threshold or symbol in positions or symbol in exited:
                continue
            bar = _bar(frames.get(symbol), day)
            if bar is None:
                continue
            price = float(bar["open"])
            rules = infer_market_rules(symbol)
            lot = max(cfg.min_lot, rules.min_lot)
            budget = cash * cfg.position_pct
            quantity = int(budget / price / lot) * lot
            while quantity >= lot:
                fee = estimate_trade_fee(quantity, price, "buy", costs)
                if quantity * price + fee <= budget:
                    break
                quantity -= lot
            if quantity < lot:
                continue
            positions[symbol] = _Position(symbol, day.strftime("%Y-%m-%d"), price, quantity, fee)
            cash -= quantity * price + fee
        for symbol, pos in list(positions.items()):
            bar = _bar(frames.get(symbol), day)
            if bar is None or not _can_sell(pos, day, cfg):
                continue
            stop = pos.entry_price * (1 - cfg.stop_loss_pct)
            target = pos.entry_price * (1 + cfg.take_profit_pct)
            if bar["open"] <= stop:
                price, reason = float(bar["open"]), "stop_loss"
            elif bar["open"] >= target:
                price, reason = float(bar["open"]), "take_profit"
            elif bar["low"] <= stop:
                price, reason = min(float(bar["open"]), stop), "stop_loss"
            elif bar["high"] >= target:
                price, reason = max(float(bar["open"]), target), "take_profit"
            else:
                continue
            trade, proceeds = _close(pos, day, price, cfg, reason)
            trades.append(trade)
            cash += proceeds
            del positions[symbol]
        opened = [_mark(pos, frames[pos.symbol], day) for pos in positions.values()]
        nav.append(cash + sum(pos.quantity * pos.mark_price for pos in opened))
    value = nav[-1] if nav else cash
    result = BacktestResult(
        cfg, scores.index[0].strftime("%Y-%m-%d") if len(scores) else "",
        scores.index[-1].strftime("%Y-%m-%d") if len(scores) else "", cfg.initial_cash,
        round(value, 4), round((value / cfg.initial_cash - 1) * 100, 4), trades, nav,
        total_trades=len(trades), cash=round(cash, 4), open_positions=opened,
    )
    _compute_metrics(result)
    return result


def _compute_metrics(result: BacktestResult) -> None:
    wins = [trade for trade in result.trades if trade.pnl > 0]
    losses = [trade for trade in result.trades if trade.pnl <= 0]
    if result.trades:
        result.win_rate = round(len(wins) / len(result.trades), 3)
    result.avg_win_pct = round(sum(t.pnl_pct for t in wins) / len(wins), 2) if wins else 0
    result.avg_loss_pct = round(sum(abs(t.pnl_pct) for t in losses) / len(losses), 2) if losses else 0
    result.payoff_ratio = round(result.avg_win_pct / result.avg_loss_pct, 2) if result.avg_loss_pct else 0
    total_losses = abs(sum(t.pnl for t in losses))
    result.profit_factor = round(sum(t.pnl for t in wins) / total_losses, 2) if total_losses else 0
    nav = pd.Series([result.initial_cash, *result.daily_nav], dtype=float)
    returns = nav.pct_change().dropna()
    if len(returns) > 1 and returns.std() > 0:
        result.sharpe_ratio = round(float(returns.mean() / returns.std() * 252 ** 0.5), 2)
    result.max_drawdown_pct = round(float(((nav - nav.cummax()) / nav.cummax()).min()) * 100, 2)
