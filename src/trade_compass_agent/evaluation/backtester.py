"""Vectorized backtesting engine — signal-based strategy simulation.

Supports:
- A-share T+1 constraint
- Lot-size enforcement (100 shares)
- Commission + stamp duty
- Slippage model
- Position sizing
- Stop-loss / take-profit exits
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import pandas as pd

logger = logging.getLogger(__name__)


@dataclass
class BacktestConfig:
    initial_cash: float = 100_000.0
    position_pct: float = 0.10  # % of portfolio per trade
    max_positions: int = 5
    entry_threshold: float = 0.7  # composite score > threshold → buy
    exit_threshold: float = 0.3  # score < threshold → sell signal
    stop_loss_pct: float = 0.08
    take_profit_pct: float = 0.20
    commission_rate: float = 0.0003
    stamp_duty_rate: float = 0.001  # sell only
    slippage_pct: float = 0.001
    min_lot: int = 100
    t_plus_one: bool = True


@dataclass
class TradeRecord:
    symbol: str
    entry_date: str
    exit_date: str
    entry_price: float
    exit_price: float
    quantity: int
    direction: str  # "long"
    pnl: float = 0.0
    pnl_pct: float = 0.0
    exit_reason: str = ""  # signal, stop_loss, take_profit, end_of_period
    commission_total: float = 0.0


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
    # Performance metrics
    sharpe_ratio: float = 0.0
    max_drawdown_pct: float = 0.0
    win_rate: float = 0.0
    avg_win_pct: float = 0.0
    avg_loss_pct: float = 0.0
    payoff_ratio: float = 0.0
    total_trades: int = 0
    profit_factor: float = 0.0


@dataclass
class _Position:
    symbol: str
    entry_date: str
    entry_price: float
    quantity: int
    stop_loss: float
    take_profit: float


def run_backtest(
    signals: pd.DataFrame,
    prices: dict[str, pd.DataFrame],
    config: BacktestConfig | None = None,
) -> BacktestResult:
    """Run vectorized backtest over signal scores and price data.

    Args:
        signals: DataFrame with index=date, columns=symbols, values=scores [0,1]
        prices: {symbol: DataFrame with columns [open, high, low, close, volume]}
        config: Backtest configuration

    Returns:
        BacktestResult with trades, NAV curve, and performance metrics
    """
    cfg = config or BacktestConfig()
    cash = cfg.initial_cash
    positions: list[_Position] = []
    trades: list[TradeRecord] = []
    daily_nav: list[float] = []
    dates = signals.index.tolist()

    for i, current_date in enumerate(dates):
        date_str = str(current_date)[:10]

        # Check exits first
        positions, new_trades, exit_cash = _check_exits(
            positions, prices, date_str, cfg
        )
        cash += exit_cash
        trades.extend(new_trades)

        # Check signal-based exits (score below threshold)
        remaining: list[_Position] = []
        for pos in positions:
            if pos.symbol in signals.columns:
                score = signals.loc[current_date, pos.symbol]
                if score < cfg.exit_threshold:
                    trade, proceeds = _close_position(pos, prices, date_str, cfg, "signal")
                    if trade:
                        trades.append(trade)
                        cash += proceeds
                    continue
            remaining.append(pos)
        positions = remaining

        # Check entries (if capacity)
        if len(positions) < cfg.max_positions:
            candidates = []
            for symbol in signals.columns:
                if any(p.symbol == symbol for p in positions):
                    continue
                score = signals.loc[current_date, symbol]
                if score > cfg.entry_threshold:
                    candidates.append((symbol, score))

            candidates.sort(key=lambda x: x[1], reverse=True)
            for symbol, _ in candidates:
                if len(positions) >= cfg.max_positions:
                    break
                pos = _open_position(symbol, prices, date_str, cash, cfg)
                if pos:
                    buy_cost = pos.quantity * pos.entry_price * (1 + cfg.commission_rate + cfg.slippage_pct)
                    cash -= buy_cost
                    positions.append(pos)

        # Record daily NAV
        portfolio_value = cash + sum(
            _current_price(p.symbol, prices, date_str) * p.quantity
            for p in positions
        )
        daily_nav.append(portfolio_value)

    # Force close remaining positions on last day
    for pos in positions:
        last_date = str(dates[-1])[:10] if dates else ""
        trade, proceeds = _close_position(pos, prices, last_date, cfg, "end_of_period")
        if trade:
            trades.append(trade)
            cash += proceeds
    positions = []

    final_value = cash
    total_return = (final_value - cfg.initial_cash) / cfg.initial_cash * 100

    result = BacktestResult(
        config=cfg,
        start_date=str(dates[0])[:10] if dates else "",
        end_date=str(dates[-1])[:10] if dates else "",
        initial_cash=cfg.initial_cash,
        final_value=round(final_value, 2),
        total_return_pct=round(total_return, 2),
        trades=trades,
        daily_nav=daily_nav,
        total_trades=len(trades),
    )

    _compute_metrics(result)
    return result


def _open_position(
    symbol: str,
    prices: dict[str, pd.DataFrame],
    date_str: str,
    cash: float,
    cfg: BacktestConfig,
) -> _Position | None:
    """Open a position with T+1 and lot-size constraints."""
    price = _current_price(symbol, prices, date_str)
    if price <= 0:
        return None

    budget = cash * cfg.position_pct
    entry_price = price * (1 + cfg.slippage_pct)
    quantity = int(budget / entry_price / cfg.min_lot) * cfg.min_lot

    if quantity < cfg.min_lot:
        return None

    cost = quantity * entry_price * (1 + cfg.commission_rate)
    if cost > cash:
        return None

    return _Position(
        symbol=symbol,
        entry_date=date_str,
        entry_price=round(entry_price, 3),
        quantity=quantity,
        stop_loss=round(entry_price * (1 - cfg.stop_loss_pct), 3),
        take_profit=round(entry_price * (1 + cfg.take_profit_pct), 3),
    )


def _check_exits(
    positions: list[_Position],
    prices: dict[str, pd.DataFrame],
    date_str: str,
    cfg: BacktestConfig,
) -> tuple[list[_Position], list[TradeRecord], float]:
    """Check stop-loss and take-profit for all positions."""
    remaining: list[_Position] = []
    new_trades: list[TradeRecord] = []
    exit_cash = 0.0

    for pos in positions:
        price = _current_price(pos.symbol, prices, date_str)
        if price <= 0:
            remaining.append(pos)
            continue

        if price <= pos.stop_loss:
            trade, proceeds = _close_position(pos, prices, date_str, cfg, "stop_loss")
            if trade:
                new_trades.append(trade)
                exit_cash += proceeds
        elif price >= pos.take_profit:
            trade, proceeds = _close_position(pos, prices, date_str, cfg, "take_profit")
            if trade:
                new_trades.append(trade)
                exit_cash += proceeds
        else:
            remaining.append(pos)

    return remaining, new_trades, exit_cash


def _close_position(
    pos: _Position,
    prices: dict[str, pd.DataFrame],
    date_str: str,
    cfg: BacktestConfig,
    reason: str,
) -> tuple[TradeRecord | None, float]:
    """Close a position and return trade record + cash proceeds."""
    price = _current_price(pos.symbol, prices, date_str)
    if price <= 0:
        price = pos.entry_price

    exit_price = price * (1 - cfg.slippage_pct)
    gross_proceeds = pos.quantity * exit_price
    commission = gross_proceeds * cfg.commission_rate
    stamp_duty = gross_proceeds * cfg.stamp_duty_rate
    net_proceeds = gross_proceeds - commission - stamp_duty

    entry_cost = pos.quantity * pos.entry_price
    pnl = net_proceeds - entry_cost
    pnl_pct = pnl / entry_cost * 100

    trade = TradeRecord(
        symbol=pos.symbol,
        entry_date=pos.entry_date,
        exit_date=date_str,
        entry_price=pos.entry_price,
        exit_price=round(exit_price, 3),
        quantity=pos.quantity,
        direction="long",
        pnl=round(pnl, 2),
        pnl_pct=round(pnl_pct, 2),
        exit_reason=reason,
        commission_total=round(commission + stamp_duty + pos.quantity * pos.entry_price * cfg.commission_rate, 2),
    )
    return trade, net_proceeds


def _current_price(symbol: str, prices: dict[str, pd.DataFrame], date_str: str) -> float:
    """Get close price for symbol on date. Returns 0 if unavailable."""
    df = prices.get(symbol)
    if df is None or df.empty:
        return 0.0
    # Try exact date match or closest prior
    if "date" in df.columns:
        row = df[df["date"] <= date_str].tail(1)
    else:
        row = df.tail(1)
    if row.empty:
        return 0.0
    return float(row.iloc[0]["close"])


def _compute_metrics(result: BacktestResult) -> None:
    """Compute performance metrics from trades and NAV."""
    if not result.trades:
        return

    wins = [t for t in result.trades if t.pnl > 0]
    losses = [t for t in result.trades if t.pnl <= 0]

    result.win_rate = round(len(wins) / len(result.trades), 3) if result.trades else 0
    result.avg_win_pct = round(sum(t.pnl_pct for t in wins) / len(wins), 2) if wins else 0
    result.avg_loss_pct = round(sum(abs(t.pnl_pct) for t in losses) / len(losses), 2) if losses else 0
    result.payoff_ratio = round(result.avg_win_pct / result.avg_loss_pct, 2) if result.avg_loss_pct > 0 else 0

    total_wins = sum(t.pnl for t in wins)
    total_losses = abs(sum(t.pnl for t in losses))
    result.profit_factor = round(total_wins / total_losses, 2) if total_losses > 0 else 0

    # Sharpe and drawdown from NAV
    if len(result.daily_nav) > 1:
        nav_series = pd.Series(result.daily_nav)
        returns = nav_series.pct_change().dropna()
        if len(returns) > 0 and returns.std() > 0:
            result.sharpe_ratio = round(float(returns.mean() / returns.std() * (252 ** 0.5)), 2)

        peak = nav_series.expanding().max()
        drawdown = (nav_series - peak) / peak
        result.max_drawdown_pct = round(float(drawdown.min()) * 100, 2)
