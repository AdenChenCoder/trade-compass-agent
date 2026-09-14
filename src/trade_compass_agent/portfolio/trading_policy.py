"""Shared autonomous-trading state and paper-account buying power."""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import json
import math
from pathlib import Path
from typing import TYPE_CHECKING

from trade_compass_agent.concurrency import atomic_write, get_path_lock
from trade_compass_agent.domain import AccountKind
from trade_compass_agent.portfolio.accounts import AccountStore

if TYPE_CHECKING:
    from trade_compass_agent.portfolio.simulator import PaperPortfolio


class TradeRejected(ValueError):
    def __init__(self, message: str, code: str, **details) -> None:
        super().__init__(message)
        self.payload = {"error": message, "code": code, "trade_rejected": True, **details}


@contextmanager
def portfolio_transaction(ledger_path: Path):
    """Serialize ledger commits and switch changes across threads and processes."""
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    with get_path_lock(ledger_path):
        with ledger_path.with_suffix(".lock").open("a") as lock_file:
            fcntl.flock(lock_file, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file, fcntl.LOCK_UN)


class AutonomousTradingStore:
    def __init__(self, data_dir: Path) -> None:
        self.path = data_dir / "autonomous_trading.json"
        self.ledger_path = data_dir / "paper_trades.jsonl"

    def read(self) -> bool:
        if not self.path.exists():
            return False
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        enabled = payload.get("enabled") if isinstance(payload, dict) else None
        if type(enabled) is not bool:
            raise ValueError("自主交易设置无效，请在模拟持仓页重新设置")
        return enabled

    def set_enabled(self, enabled: bool) -> bool:
        if type(enabled) is not bool:
            raise ValueError("enabled 必须为布尔值")
        with portfolio_transaction(self.ledger_path):
            atomic_write(self.path, json.dumps({"enabled": enabled}) + "\n")
        return enabled


def buying_power(portfolio: PaperPortfolio, data_dir: Path, account: AccountKind) -> float:
    matches = [a for a in AccountStore(data_dir / "accounts.json").list() if a.kind == account]
    if len(matches) != 1:
        raise ValueError(f"账户类型 {account.value} 对应 {len(matches)} 个账户，无法确定可用资金")
    capital = matches[0].capital
    if not math.isfinite(capital) or capital < 0:
        raise ValueError(f"账户 {matches[0].name} 的总资金无效")
    return portfolio.cash_balance(account, capital)


def account_balances(portfolio: PaperPortfolio, data_dir: Path) -> list[dict]:
    balances = []
    for account in AccountStore(data_dir / "accounts.json").list():
        item = {"id": account.id, "account": account.kind.value, "capital": account.capital}
        try:
            item["available_cash"] = buying_power(portfolio, data_dir, account.kind)
        except ValueError as exc:
            item.update(available_cash=None, cash_error=str(exc))
        balances.append(item)
    return balances
