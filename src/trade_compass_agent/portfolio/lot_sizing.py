"""Map fractional rebalance intents to executable A-share lot sizes."""

from __future__ import annotations

from typing import Literal

Intent = Literal["reduce_third", "reduce_half", "sell_all", "hold"]

REVIEW_SKILL = "contextual-take-profit"

LOT_SIZING_RULES_MD = """\
## A股最小手数约束（卖出建议必遵）

- 主板/创业板：100 股整数倍；科创板：200 股；可转债：10 张
- **持仓 ≤ min_lot**：无法部分减仓，只能「全部卖出」或「继续持有」
- **禁止**输出卖 33/50/66 股等不可执行数量；禁止对 100 股说「减 1/3」「减半仓」
- 给出减仓/止盈/止损前：调用 `analyze_portfolio` 或 `get_market_constraints`，\
以 `rebalance_hint` / `exit_review` / `is_min_lot` / `sell_qty` 为准
- 百分比规则（减 1/3、减半）仅是**意图**；必须映射为整数手数后再表述

## 止盈审查规则（禁止机械止盈）

- **禁止仅凭涨幅**（+15%/+20%/+25% 等）直接建议止盈或减仓
- 浮盈达审查线时，`exit_review.review_only=true` → 仅表示「进入评估」，**不是卖出指令**
- 必须 `load_skill(contextual-take-profit)`（内含 8 维评分表），从板块热度、趋势、量能、资金、K线、RSI 等维度综合判断
- 仅当多数维度指向「应该卖（止盈）」时，才给出可执行 `sell_qty`；主升未破坏、板块仍强 → 持有或收紧止损
- `rebalance_hint` 仅用于**止损**侧的可执行映射；止盈侧以多维评估结论为准
"""


def pnl_exit_review_candidate(
    pnl_pct: float,
    *,
    review_threshold: float = 15.0,
) -> dict | None:
    """Return review-only flag when gain qualifies for contextual take-profit evaluation."""
    if pnl_pct < review_threshold:
        return None
    if pnl_pct >= 30.0:
        tier = "pnl_review_high"
    elif pnl_pct >= 20.0:
        tier = "pnl_review_mid"
    else:
        tier = "pnl_review"
    return {
        "review_only": True,
        "trigger": tier,
        "pnl_pct": round(pnl_pct, 2),
        "skill": REVIEW_SKILL,
        "note": (
            f"浮盈{pnl_pct:.1f}%达审查线，须 load_skill({REVIEW_SKILL}) "
            "8维评分评估是否止盈；禁止仅凭涨幅减仓"
        ),
    }


def format_pnl_alert(symbol: str, quantity: int, pnl_pct: float) -> str | None:
    """Build a lot-aware alert string for threshold P&L, or None if no action."""
    from trade_compass_agent.portfolio.market_rules import infer_market_rules

    rules = infer_market_rules(symbol)
    stop = suggest_rebalance_for_pnl(quantity, rules.min_lot, pnl_pct)
    if stop:
        label = "浮亏" if pnl_pct < 0 else "止损"
        return f"{symbol} {label}{pnl_pct:.1f}%：{stop['note']}"
    review = pnl_exit_review_candidate(pnl_pct)
    if review:
        return f"{symbol} {review['note']}"
    if pnl_pct <= -8.0:
        if quantity <= rules.min_lot:
            return f"{symbol} 亏损{pnl_pct:.1f}%（{quantity}股=1手，只能全卖或持有）"
        return f"{symbol} 亏损{pnl_pct:.1f}%"
    return None


def valid_sell_quantities(quantity: int, min_lot: int) -> list[int]:
    """Sell sizes (multiples of min_lot) that leave 0 or >= min_lot remaining."""
    if quantity <= 0 or min_lot <= 0:
        return []
    out: list[int] = []
    for sell in range(min_lot, quantity + 1, min_lot):
        remain = quantity - sell
        if remain == 0 or remain >= min_lot:
            out.append(sell)
    return out


def _round_down_to_lot(shares: float, min_lot: int) -> int:
    if min_lot <= 0:
        return 0
    return int(shares // min_lot) * min_lot


def map_intent_to_sell(quantity: int, min_lot: int, intent: Intent) -> dict:
    """Translate reduce-third / half / sell-all into an executable sell quantity."""
    valid = valid_sell_quantities(quantity, min_lot)
    if not valid:
        return {
            "intent": intent,
            "executable": False,
            "sell_qty": 0,
            "remaining_qty": quantity,
            "action": "hold",
            "note": "无可执行卖出数量",
        }

    if quantity <= min_lot:
        return {
            "intent": intent,
            "executable": intent == "sell_all",
            "sell_qty": quantity if intent == "sell_all" else 0,
            "remaining_qty": 0 if intent == "sell_all" else quantity,
            "action": "sell_all_or_hold",
            "note": f"持仓{quantity}股=最小手数{min_lot}股，无法部分减仓，只能全部卖出或继续持有",
        }

    if intent == "sell_all":
        return {
            "intent": intent,
            "executable": True,
            "sell_qty": quantity,
            "remaining_qty": 0,
            "action": "sell_all",
            "note": f"卖出全部 {quantity} 股",
        }

    if intent == "hold":
        return {
            "intent": intent,
            "executable": True,
            "sell_qty": 0,
            "remaining_qty": quantity,
            "action": "hold",
            "note": "继续持有",
        }

    if intent == "reduce_half":
        target = _round_down_to_lot(quantity / 2, min_lot)
    else:  # reduce_third
        target = _round_down_to_lot(quantity / 3, min_lot)

    if target >= min_lot and target in valid:
        remain = quantity - target
        return {
            "intent": intent,
            "executable": True,
            "sell_qty": target,
            "remaining_qty": remain,
            "action": "partial_sell",
            "note": f"卖出 {target} 股，剩余 {remain} 股",
        }

    # Fallback: smallest partial sell, or sell-all-or-hold for min-lot-adjacent sizes
    partial = [v for v in valid if v < quantity]
    if partial:
        sell = partial[0]
        return {
            "intent": intent,
            "executable": True,
            "sell_qty": sell,
            "remaining_qty": quantity - sell,
            "action": "partial_sell_fallback",
            "note": f"无法精确{'减1/3' if intent == 'reduce_third' else '减半'}，最小可执行部分卖出 {sell} 股",
        }

    return {
        "intent": intent,
        "executable": False,
        "sell_qty": 0,
        "remaining_qty": quantity,
        "action": "sell_all_or_hold",
        "note": f"无法部分减仓，只能全部卖出 {quantity} 股或继续持有",
    }


def suggest_rebalance_for_pnl(
    quantity: int,
    min_lot: int,
    pnl_pct: float,
    *,
    stop_loss_half: float = -10.0,
    stop_loss_full: float = -15.0,
) -> dict | None:
    """Return executable rebalance suggestion when stop-loss thresholds are hit."""
    intent: Intent | None = None
    trigger = None

    if pnl_pct <= stop_loss_full:
        intent, trigger = "sell_all", "stop_loss_full"
    elif pnl_pct <= stop_loss_half:
        intent, trigger = "reduce_half", "stop_loss_half"

    if intent is None:
        return None

    mapped = map_intent_to_sell(quantity, min_lot, intent)
    mapped["trigger"] = trigger
    mapped["pnl_pct"] = round(pnl_pct, 2)
    return mapped
