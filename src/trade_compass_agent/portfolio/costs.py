"""Shared paper-trading and research cost model (slippage charged once)."""

from trade_compass_agent.config import TradingCostConfig


def estimate_trade_fee(quantity: int, price: float, side: str, costs: TradingCostConfig) -> float:
    gross = quantity * price
    commission = max(gross * costs.commission_rate, costs.min_commission)
    transfer = gross * costs.transfer_fee_rate
    stamp = gross * costs.stamp_duty_rate if side == "sell" else 0.0
    slippage = gross * costs.slippage_bps / 10_000
    return round(commission + transfer + stamp + slippage, 4)
