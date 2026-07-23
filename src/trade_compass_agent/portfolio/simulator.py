from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, replace
import hashlib
import json
import logging
from datetime import datetime
import os
from pathlib import Path
import tempfile

from trade_compass_agent.config import TradingCostConfig
from trade_compass_agent.data.network import run_with_timeout
from trade_compass_agent.domain import AccountKind, PaperTrade, PortfolioPosition


logger = logging.getLogger(__name__)
_MARKET_REFRESH_TIMEOUT_SECONDS = 15.0
_SINA_BATCH_TIMEOUT_SECONDS = 6.0


@dataclass(frozen=True)
class AccountSummary:
    account: AccountKind
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


@dataclass(frozen=True)
class RealizedTrade:
    account: AccountKind
    symbol: str
    quantity: int
    entry_price: float
    exit_price: float
    pnl: float
    fees: float
    opened_at: datetime
    closed_at: datetime
    entry_trade_id: str = ""
    exit_trade_id: str = ""
    decision_id: str | None = None


@dataclass
class _Lot:
    quantity: int
    price: float
    timestamp: datetime
    buy_fee: float
    trade_id: str
    decision_id: str | None


class PaperPortfolio:
    def __init__(self, costs: TradingCostConfig | None = None) -> None:
        self.trades: list[PaperTrade] = []
        self.costs = costs or TradingCostConfig()

    def record(self, trade: PaperTrade) -> None:
        self.trades.append(trade)

    def validate_trade(self, trade: PaperTrade, *, skip_t1: bool = False) -> tuple[bool, str]:
        if trade.quantity <= 0 or trade.price <= 0:
            return False, "数量与价格必须为正"
        if trade.suspended:
            return False, "停牌标的不可交易"
        if trade.previous_close:
            up_limit = round(trade.previous_close * (1 + trade.price_limit_pct), 2)
            down_limit = round(trade.previous_close * (1 - trade.price_limit_pct), 2)
            if trade.side == "buy" and trade.price > up_limit:
                return False, f"买入价超过涨停价（涨停价 {up_limit}）"
            if trade.side == "sell" and trade.price < down_limit:
                return False, f"卖出价低于跌停价（跌停价 {down_limit}）"
        if trade.side == "sell":
            position_qty = self._position_qty(trade.symbol, trade.account)
            if trade.quantity > position_qty:
                return False, f"卖出数量超过持仓（当前 {position_qty}）"
            if not skip_t1 and not trade.is_t0 and not self._can_sell_t_plus_one(trade):
                return False, "T+1：当日买入的份额下一交易日才可卖出"
        return True, "ok"

    def positions(self) -> list[PortfolioPosition]:
        lots, _realized, _fees = self._lots_and_realized()
        last_price = self._last_prices()
        names = getattr(self, "_symbol_names", {})
        result: list[PortfolioPosition] = []
        for (symbol, account), open_lots in lots.items():
            quantity = sum(lot.quantity for lot in open_lots)
            if quantity <= 0:
                continue
            raw_cost = sum(lot.quantity * lot.price for lot in open_lots)
            avg_cost = raw_cost / quantity
            price = last_price.get((symbol, account), avg_cost)
            market_value = quantity * price
            result.append(
                PortfolioPosition(
                    symbol=symbol,
                    account=account,
                    quantity=quantity,
                    avg_cost=round(avg_cost, 3),
                    last_price=round(price, 3),
                    market_value=round(market_value, 2),
                    unrealized_pnl=round(market_value - raw_cost, 2),
                    name=names.get(symbol, ""),
                    price_source="last_trade",
                    price_is_fresh=False,
                    opened_at=min(lot.timestamp for lot in open_lots),
                )
            )
        return result

    def positions_by_account(self) -> dict[AccountKind, list[PortfolioPosition]]:
        grouped: dict[AccountKind, list[PortfolioPosition]] = defaultdict(list)
        for position in self.positions():
            grouped[position.account].append(position)
        return grouped

    def account_summaries(self) -> list[AccountSummary]:
        summaries: list[AccountSummary] = []
        grouped = self.positions_by_account()
        realized = self.realized_trades()
        fees_by_account = self.fees_by_account()
        for account in AccountKind:
            items = grouped.get(account, [])
            market_value = sum(item.quantity * item.last_price for item in items)
            cost_basis = sum(item.quantity * item.avg_cost for item in items)
            account_realized = [item for item in realized if item.account == account]
            realized_pnl = sum(item.pnl for item in account_realized)
            wins = [item.pnl for item in account_realized if item.pnl > 0]
            losses = [item.pnl for item in account_realized if item.pnl <= 0]
            avg_win = sum(wins) / len(wins) if wins else 0.0
            avg_loss = abs(sum(losses) / len(losses)) if losses else 0.0
            summaries.append(
                AccountSummary(
                    account=account,
                    position_count=len(items),
                    market_value=round(market_value, 2),
                    cost_basis=round(cost_basis, 2),
                    unrealized_pnl=round(market_value - cost_basis, 2),
                    realized_pnl=round(realized_pnl, 2),
                    fees=round(fees_by_account.get(account, 0.0), 2),
                    wins=len(wins),
                    losses=len(losses),
                    win_rate=round(len(wins) / len(account_realized), 4) if account_realized else 0.0,
                    payoff_ratio=round(avg_win / avg_loss, 4) if avg_loss else 0.0,
                    max_drawdown=round(self._max_drawdown(account), 4),
                )
            )
        return summaries

    def realized_trades(self) -> list[RealizedTrade]:
        _lots, realized, _fees = self._lots_and_realized()
        return realized

    def fees_by_account(self) -> dict[AccountKind, float]:
        _lots, _realized, fees = self._lots_and_realized()
        return fees

    def _position_qty(self, symbol: str, account: AccountKind) -> int:
        qty = 0
        for trade in self.trades:
            if trade.symbol == symbol and trade.account == account:
                qty += trade.quantity if trade.side == "buy" else -trade.quantity
        return max(qty, 0)

    def _can_sell_t_plus_one(self, sell_trade: PaperTrade) -> bool:
        sell_day = sell_trade.timestamp.date()
        bought_today = 0
        for trade in self.trades:
            if (
                trade.symbol == sell_trade.symbol
                and trade.account == sell_trade.account
                and trade.side == "buy"
                and trade.timestamp.date() == sell_day
            ):
                bought_today += trade.quantity
        return sell_trade.quantity <= self._position_qty(sell_trade.symbol, sell_trade.account) - bought_today

    def estimate_fee(self, trade: PaperTrade) -> float:
        gross = trade.quantity * trade.price
        commission = max(gross * self.costs.commission_rate, self.costs.min_commission)
        transfer = gross * self.costs.transfer_fee_rate
        stamp = gross * self.costs.stamp_duty_rate if trade.side == "sell" else 0.0
        slippage = gross * (self.costs.slippage_bps / 10_000)
        return round(commission + transfer + stamp + slippage, 4)

    def _lots_and_realized(self) -> tuple[dict[tuple[str, AccountKind], list[_Lot]], list[RealizedTrade], dict[AccountKind, float]]:
        lots: dict[tuple[str, AccountKind], list[_Lot]] = defaultdict(list)
        realized: list[RealizedTrade] = []
        fees: dict[AccountKind, float] = defaultdict(float)
        for trade in sorted(self.trades, key=lambda item: item.timestamp):
            key = (trade.symbol, trade.account)
            fee = self.estimate_fee(trade)
            fees[trade.account] += fee
            if trade.side == "buy":
                lots[key].append(
                    _Lot(
                        trade.quantity,
                        trade.price,
                        trade.timestamp,
                        fee,
                        trade.trade_id,
                        trade.decision_id,
                    )
                )
                continue

            remaining = trade.quantity
            sell_fee_remaining = fee
            while remaining > 0 and lots[key]:
                lot = lots[key][0]
                close_qty = min(remaining, lot.quantity)
                buy_fee_share = lot.buy_fee * (close_qty / lot.quantity)
                sell_fee_share = sell_fee_remaining * (close_qty / remaining)
                pnl = (trade.price - lot.price) * close_qty - buy_fee_share - sell_fee_share
                realized.append(
                    RealizedTrade(
                        account=trade.account,
                        symbol=trade.symbol,
                        quantity=close_qty,
                        entry_price=lot.price,
                        exit_price=trade.price,
                        pnl=round(pnl, 2),
                        fees=round(buy_fee_share + sell_fee_share, 2),
                        opened_at=lot.timestamp,
                        closed_at=trade.timestamp,
                        entry_trade_id=lot.trade_id,
                        exit_trade_id=trade.trade_id,
                        decision_id=lot.decision_id,
                    )
                )
                lot.quantity -= close_qty
                lot.buy_fee -= buy_fee_share
                remaining -= close_qty
                sell_fee_remaining -= sell_fee_share
                if lot.quantity <= 0:
                    lots[key].pop(0)
        return lots, realized, fees

    def _last_prices(self) -> dict[tuple[str, AccountKind], float]:
        last_price: dict[tuple[str, AccountKind], float] = {}
        for trade in self.trades:
            last_price[(trade.symbol, trade.account)] = trade.price
        return last_price

    def refresh_market_prices(self, provider) -> dict[str, float]:
        """Refresh positions with latest market prices.

        Tries a fast batch quote via Sina first (sub-second for any number of symbols).
        Falls back to provider.get_bars() concurrently if Sina fails.
        """
        held_symbols = {trade.symbol for trade in self.trades}
        if not held_symbols:
            self._market_prices = {}
            return {}

        try:
            sina_result = run_with_timeout(
                lambda: self._try_sina_batch_quote(held_symbols),
                timeout=_SINA_BATCH_TIMEOUT_SECONDS,
                description="sina-batch-quote",
            )
        except TimeoutError:
            logger.warning("Sina batch quote timed out; falling back to bar providers")
            sina_result = None
        if sina_result:
            prices, names = sina_result
            self._market_prices = prices
            self._symbol_names = names
            return prices

        # Fallback: concurrent get_bars
        from concurrent.futures import ThreadPoolExecutor, wait

        def _fetch(symbol: str) -> tuple[str, float | None]:
            try:
                bars = provider.get_bars(symbol, timeframe="1d", limit=1)
                if bars:
                    return symbol, bars[-1].close
            except Exception:
                pass
            return symbol, None

        refreshed = {}
        pool = ThreadPoolExecutor(max_workers=min(8, len(held_symbols)))
        futures = {pool.submit(_fetch, sym): sym for sym in held_symbols}
        done, pending = wait(futures, timeout=_MARKET_REFRESH_TIMEOUT_SECONDS)
        for fut in done:
            sym, price = fut.result()
            if price is not None:
                refreshed[sym] = price
        if pending:
            for fut in pending:
                fut.cancel()
            logger.warning(
                "Market price refresh timed out after %.0fs (%d/%d symbols pending); "
                "using last trade prices for missing quotes",
                _MARKET_REFRESH_TIMEOUT_SECONDS,
                len(pending),
                len(futures),
            )
        pool.shutdown(wait=False, cancel_futures=True)

        self._market_prices = refreshed
        return refreshed

    @staticmethod
    def _try_sina_batch_quote(symbols: set[str]) -> tuple[dict[str, float], dict[str, str]] | None:
        """Fetch real-time prices and names from Sina Finance in one HTTP call.

        Returns (prices, names) or None on failure.
        """
        import requests

        def _sina_code(s: str) -> str:
            if s.startswith(("6", "5", "9", "11")):
                return f"sh{s}"
            return f"sz{s}"

        code_list = ",".join(_sina_code(s) for s in symbols)
        url = f"https://hq.sinajs.cn/list={code_list}"
        try:
            resp = requests.get(
                url,
                headers={"Referer": "https://finance.sina.com.cn"},
                timeout=5,
            )
            if resp.status_code != 200:
                return None
        except Exception:
            return None

        prices: dict[str, float] = {}
        names: dict[str, str] = {}
        for line in resp.text.strip().split("\n"):
            if "=" not in line:
                continue
            var_part, data_part = line.split("=", 1)
            code_raw = var_part.split("_")[-1]  # e.g. sz002938
            symbol = code_raw[2:]  # strip sh/sz prefix
            fields = data_part.strip().strip('";').split(",")
            if len(fields) >= 4:
                if fields[0]:
                    names[symbol] = fields[0]
                try:
                    price = float(fields[3])
                    if price > 0:
                        prices[symbol] = price
                except (ValueError, IndexError):
                    continue

        return (prices, names) if prices or names else None

    def resolve_names(self) -> None:
        """Fetch stock names from Sina for all held symbols (no-op if already cached)."""
        if getattr(self, "_symbol_names", None):
            return
        held = {t.symbol for t in self.trades}
        if not held:
            return
        result = self._try_sina_batch_quote(held)
        if result:
            _prices, names = result
            self._symbol_names = names

    def positions_with_market_prices(self, provider=None) -> list[PortfolioPosition]:
        """Get positions using latest market prices instead of last trade price.

        If provider given, refreshes prices first. Otherwise uses cached prices.
        """
        if provider:
            self.refresh_market_prices(provider)

        lots, _realized, _fees = self._lots_and_realized()
        market_prices = getattr(self, "_market_prices", {})
        last_price = self._last_prices()
        names = getattr(self, "_symbol_names", {})

        result: list[PortfolioPosition] = []
        for (symbol, account), open_lots in lots.items():
            quantity = sum(lot.quantity for lot in open_lots)
            if quantity <= 0:
                continue
            raw_cost = sum(lot.quantity * lot.price for lot in open_lots)
            avg_cost = raw_cost / quantity
            if symbol in market_prices:
                price = market_prices[symbol]
                price_source = "market"
                price_is_fresh = True
            elif (symbol, account) in last_price:
                price = last_price[(symbol, account)]
                price_source = "last_trade"
                price_is_fresh = False
            else:
                price = avg_cost
                price_source = "avg_cost_fallback"
                price_is_fresh = False
            market_value = quantity * price
            result.append(
                PortfolioPosition(
                    symbol=symbol,
                    account=account,
                    quantity=quantity,
                    avg_cost=round(avg_cost, 3),
                    last_price=round(price, 3),
                    market_value=round(market_value, 2),
                    unrealized_pnl=round(market_value - raw_cost, 2),
                    name=names.get(symbol, ""),
                    price_source=price_source,
                    price_is_fresh=price_is_fresh,
                    opened_at=min(lot.timestamp for lot in open_lots),
                )
            )
        return result

    def _max_drawdown(self, account: AccountKind) -> float:
        equity = 0.0
        peak = 0.0
        max_dd = 0.0
        for trade in sorted((item for item in self.trades if item.account == account), key=lambda item: item.timestamp):
            gross = trade.quantity * trade.price
            fee = self.estimate_fee(trade)
            equity += -gross - fee if trade.side == "buy" else gross - fee
            peak = max(peak, equity)
            if peak > 0:
                max_dd = min(max_dd, (equity - peak) / peak)
        return max_dd


class JsonPaperPortfolio(PaperPortfolio):
    def __init__(self, path: Path, costs: TradingCostConfig | None = None) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        super().__init__(costs=costs)
        self.trades = self._load()
        from trade_compass_agent.concurrency import get_path_lock
        self._lock = get_path_lock(self.path)

    def record(self, trade: PaperTrade, *, skip_t1: bool = False) -> None:
        with self._lock:
            self.trades = self._load()
            ok, message = self.validate_trade(trade, skip_t1=skip_t1)
            if not ok:
                raise ValueError(message)
            super().record(trade)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(_trade_payload(trade), ensure_ascii=False) + "\n")

    def persist_trade_metadata(self, decision_ids_by_trade: dict[str, str]) -> bool:
        """Atomically persist IDs for legacy rows without changing trade semantics."""
        with self._lock:
            raw_rows = [
                json.loads(line)
                for line in self.path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ] if self.path.exists() else []
            needs_rewrite = any(
                not row.get("trade_id")
                or (
                    row.get("side") == "buy"
                    and decision_ids_by_trade.get(str(row.get("trade_id") or ""))
                    and not row.get("decision_id")
                )
                for row in raw_rows
            )
            if not needs_rewrite:
                return False

            trades = self._load()
            enriched = [
                replace(
                    trade,
                    decision_id=trade.decision_id or decision_ids_by_trade.get(trade.trade_id),
                )
                for trade in trades
            ]
            content = "\n".join(
                json.dumps(_trade_payload(trade), ensure_ascii=False) for trade in enriched
            ) + "\n"
            fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
            fd_open = True
            try:
                os.write(fd, content.encode("utf-8"))
                os.close(fd)
                fd_open = False
                os.replace(tmp, self.path)
            except BaseException:
                if fd_open:
                    os.close(fd)
                if os.path.exists(tmp):
                    os.unlink(tmp)
                raise
            self.trades = enriched
            return True

    def _load(self) -> list[PaperTrade]:
        if not self.path.exists():
            return []
        trades: list[PaperTrade] = []
        for line_number, line in enumerate(self.path.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            raw = json.loads(line)
            trade_id = str(raw.get("trade_id") or _legacy_trade_id(raw, line_number))
            trades.append(
                PaperTrade(
                    symbol=str(raw["symbol"]),
                    account=AccountKind(raw["account"]),
                    side=raw["side"],
                    quantity=int(raw["quantity"]),
                    price=float(raw["price"]),
                    timestamp=datetime.fromisoformat(raw["timestamp"]),
                    reason=str(raw.get("reason", "")),
                    trade_id=trade_id,
                    decision_id=(str(raw["decision_id"]) if raw.get("decision_id") else None),
                    price_source=str(raw.get("price_source") or "legacy_import"),
                    price_as_of=(
                        datetime.fromisoformat(raw["price_as_of"])
                        if raw.get("price_as_of")
                        else None
                    ),
                    requested_price=(
                        float(raw["requested_price"])
                        if raw.get("requested_price") is not None
                        else None
                    ),
                    previous_close=(
                        float(raw["previous_close"]) if raw.get("previous_close") is not None else None
                    ),
                    suspended=bool(raw.get("suspended", False)),
                    is_st=bool(raw.get("is_st", False)),
                    is_t0=bool(raw.get("is_t0", False)),
                    price_limit_pct=float(raw.get("price_limit_pct", 0.10)),
                )
            )
        return trades


def _legacy_trade_id(raw: dict, line_number: int) -> str:
    """Return a stable ID for append-only legacy rows that predate trade IDs."""
    canonical = json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(f"{line_number}:{canonical}".encode("utf-8")).hexdigest()[:16]
    return f"legacy-{digest}"


def _trade_payload(trade: PaperTrade) -> dict:
    return {
        "trade_id": trade.trade_id,
        "decision_id": trade.decision_id,
        "symbol": trade.symbol,
        "account": trade.account.value if hasattr(trade.account, "value") else trade.account,
        "side": trade.side,
        "quantity": trade.quantity,
        "price": trade.price,
        "timestamp": trade.timestamp.isoformat(),
        "reason": trade.reason,
        "price_source": trade.price_source,
        "price_as_of": trade.price_as_of.isoformat() if trade.price_as_of else None,
        "requested_price": trade.requested_price,
        "previous_close": trade.previous_close,
        "suspended": trade.suspended,
        "is_st": trade.is_st,
        "is_t0": trade.is_t0,
        "price_limit_pct": trade.price_limit_pct,
    }
