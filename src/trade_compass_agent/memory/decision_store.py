"""Decision Journal — trade decision lifecycle tracking (Tier 1 Episodic).

Stores decisions through a pending, resolved, and reflected lifecycle:
- Phase A: store_decision(buy) → pending
- Phase B: resolve(sell) → resolved (with outcome PnL)
- Phase C: reflect (weekly curator) → reflected (with LLM reflection)

Storage: data/decisions.jsonl (append-mostly, atomic updates via rewrite).
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

logger = logging.getLogger(__name__)


@dataclass
class TradeDecision:
    id: str
    symbol: str
    side: str
    quantity: int
    price: float
    account: str
    reasoning: str
    market_context: str
    decided_at: str
    source_skills: list[str] = field(default_factory=list)
    status: str = "pending"
    outcome_price: float | None = None
    outcome_pnl_pct: float | None = None
    holding_days: int | None = None
    reflection: str | None = None
    resolved_at: str | None = None
    entry_trade_id: str | None = None
    resolved_quantity: int = 0
    outcome_cost_basis: float | None = None
    outcome_proceeds: float | None = None
    outcome_fees: float | None = None
    outcome_net_pnl: float | None = None
    outcome_net_pnl_pct: float | None = None
    outcome_trade_ids: list[str] = field(default_factory=list)
    outcome_source: str | None = None
    reconciliation_status: str | None = None
    reflection_stale: bool = False
    reflection_history: list[str] = field(default_factory=list)


class DecisionStore:
    """JSONL-backed store for trade decisions with lifecycle management."""

    def __init__(self, data_dir: Path) -> None:
        self._file = data_dir / "decisions.jsonl"
        data_dir.mkdir(parents=True, exist_ok=True)

    def store_decision(
        self,
        symbol: str,
        side: str,
        quantity: int,
        price: float,
        account: str,
        reasoning: str = "",
        market_context: str = "",
        source_skills: list[str] | None = None,
        decision_id: str | None = None,
        entry_trade_id: str | None = None,
        decided_at: str | None = None,
    ) -> TradeDecision:
        """Record a new trade decision as pending."""
        decision = TradeDecision(
            id=decision_id or str(uuid4())[:8],
            symbol=symbol,
            side=side,
            quantity=quantity,
            price=price,
            account=account,
            reasoning=reasoning,
            market_context=market_context,
            decided_at=decided_at or datetime.now(timezone.utc).isoformat(),
            source_skills=source_skills or [],
            status="pending",
            entry_trade_id=entry_trade_id,
        )
        self._append(decision)
        logger.info("Decision recorded [%s]: %s %s %d @%.2f", decision.id, side, symbol, quantity, price)
        return decision

    def resolve(
        self,
        symbol: str,
        account: str,
        sell_price: float,
    ) -> TradeDecision | None:
        """Resolve the most recent pending buy for this symbol+account.

        Calculates PnL and marks as resolved.
        """
        decisions = self._load_all()
        target = None
        for d in reversed(decisions):
            if (
                d.status == "pending"
                and d.side == "buy"
                and d.symbol == symbol
                and d.account == account
            ):
                target = d
                break

        if target is None:
            return None

        now = datetime.now(timezone.utc)
        decided = datetime.fromisoformat(target.decided_at)
        holding = (now - decided).days

        target.status = "resolved"
        target.outcome_price = sell_price
        target.outcome_pnl_pct = round((sell_price - target.price) / target.price * 100, 2)
        target.resolved_quantity = target.quantity
        target.outcome_cost_basis = round(target.price * target.quantity, 2)
        target.outcome_proceeds = round(sell_price * target.quantity, 2)
        target.outcome_trade_ids = []
        target.outcome_source = "legacy_direct"
        target.reconciliation_status = "unverified"
        target.holding_days = holding
        target.resolved_at = now.isoformat()

        self._rewrite(decisions)
        logger.info(
            "Decision resolved [%s]: %s PnL=%.2f%% (%dd)",
            target.id, target.symbol, target.outcome_pnl_pct, holding,
        )
        return target

    def add_reflection(self, decision_id: str, reflection: str) -> bool:
        """Add reflection to a resolved decision (curator phase)."""
        decisions = self._load_all()
        for d in decisions:
            if d.id == decision_id and d.status == "resolved":
                d.reflection = reflection
                d.status = "reflected"
                d.reflection_stale = False
                self._rewrite(decisions)
                return True
        return False

    def get_pending(self, symbol: str | None = None) -> list[TradeDecision]:
        """Get all pending decisions, optionally filtered by symbol."""
        return [
            d for d in self._load_all()
            if d.status in ("pending", "partial") and (symbol is None or d.symbol == symbol)
        ]

    def get_past_context(self, symbol: str, n_same: int = 5, n_cross: int = 3) -> str:
        """Build scoped past context for prompt injection.

        - Same symbol: full decision + reflection (top n_same)
        - Cross symbol: reflection only (top n_cross)
        """
        decisions = [d for d in self._load_all() if d.status in ("resolved", "reflected")]
        if not decisions:
            return ""

        same = [d for d in decisions if d.symbol == symbol][-n_same:]
        cross = [d for d in decisions if d.symbol != symbol and d.reflection][-n_cross:]

        parts = []
        if same:
            parts.append(f"## {symbol} 历史决策 (最近 {len(same)} 条)")
            for d in reversed(same):
                line = f"- [{d.decided_at[:10]}] {d.side} {d.quantity}股 @{d.price}"
                if d.outcome_pnl_pct is not None:
                    line += f" → PnL {d.outcome_pnl_pct:+.1f}% ({d.holding_days}天)"
                if d.reasoning:
                    line += f"\n  理由: {d.reasoning}"
                if d.reflection:
                    line += f"\n  反思: {d.reflection}"
                parts.append(line)

        if cross:
            parts.append(f"\n## 跨标的教训 (最近 {len(cross)} 条)")
            for d in reversed(cross):
                parts.append(f"- [{d.symbol}] {d.reflection}")

        return "\n".join(parts)

    def search(
        self,
        symbol: str | None = None,
        side: str | None = None,
        status: str | None = None,
        limit: int = 20,
    ) -> list[TradeDecision]:
        """Search decisions with optional filters."""
        results = self._load_all()
        if symbol:
            results = [d for d in results if d.symbol == symbol]
        if side:
            results = [d for d in results if d.side == side]
        if status:
            results = [d for d in results if d.status == status]
        return results[-limit:]

    def stats(self) -> dict[str, Any]:
        """Compute decision statistics."""
        all_d = self._load_all()
        pending = [d for d in all_d if d.status == "pending"]
        partial = [d for d in all_d if d.status == "partial"]
        awaiting = [d for d in all_d if d.status == "resolved"]
        reflected = [d for d in all_d if d.status == "reflected"]
        settled = [d for d in all_d if d.status in ("resolved", "reflected") and d.outcome_pnl_pct is not None]

        base = {
            "total": len(all_d),
            "pending": len(pending),
            "partial": len(partial),
            "awaiting_reflection": len(awaiting),
            "reflected": len(reflected),
            "resolved": len(settled),
        }
        if not settled:
            return base

        wins = [d for d in settled if d.outcome_pnl_pct > 0]
        return {
            **base,
            "win_rate": round(len(wins) / len(settled) * 100, 1),
            "avg_pnl": round(sum(d.outcome_pnl_pct for d in settled) / len(settled), 2),
            "avg_holding_days": round(sum(d.holding_days or 0 for d in settled) / len(settled), 1),
        }

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _load_all(self) -> list[TradeDecision]:
        if not self._file.exists():
            return []
        decisions = []
        for line in self._file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
                decisions.append(TradeDecision(**data))
            except (json.JSONDecodeError, TypeError):
                continue
        return decisions

    def _append(self, decision: TradeDecision) -> None:
        with open(self._file, "a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(decision), ensure_ascii=False) + "\n")

    def _rewrite(self, decisions: list[TradeDecision]) -> None:
        """Atomic rewrite of the full JSONL file."""
        content = "\n".join(json.dumps(asdict(d), ensure_ascii=False) for d in decisions) + "\n"
        fd, tmp = tempfile.mkstemp(dir=self._file.parent, suffix=".tmp")
        try:
            os.write(fd, content.encode("utf-8"))
            os.close(fd)
            os.replace(tmp, self._file)
        except BaseException:
            os.close(fd) if not os.get_inheritable(fd) else None
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
