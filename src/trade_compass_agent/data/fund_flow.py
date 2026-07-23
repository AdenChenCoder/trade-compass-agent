"""Fund flow data provider — main force and sector flows via Sina MoneyFlow API."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

logger = logging.getLogger(__name__)

_TIMEOUT = 8.0
_SINA_HEADERS = {"Referer": "https://vip.stock.finance.sina.com.cn", "User-Agent": "Mozilla/5.0"}

_FUND_FLOW_CATEGORIES = frozenset({"main_force", "industry", "concept", "summary"})


def _is_cn_trading_hours() -> bool:
    """Check if current time is within China A-share trading hours (9:15-15:05 CST, weekdays)."""
    now = datetime.now(timezone(timedelta(hours=8)))
    if now.weekday() >= 5:
        return False
    t = now.hour * 100 + now.minute
    return 915 <= t <= 1505


@dataclass
class StockMainFlow:
    """Per-stock main force flow."""

    symbol: str
    name: str
    main_net_inflow: float  # 净流入（亿元）
    main_pct: float


@dataclass
class SectorFlow:
    """Sector/concept fund flow."""

    sector_name: str
    change_pct: float
    net_inflow: float  # 净流入（亿元）


class FundFlowProvider:
    """Provides main-force and sector fund flow data."""

    def get_stock_main_flow(self, limit: int = 20) -> list[StockMainFlow]:
        """Get top stocks by main force net inflow via Sina MoneyFlow API."""
        url = "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/MoneyFlow.ssl_bkzj_ssggzj"
        params = {"page": "1", "num": str(limit), "sort": "netamount", "asc": "0"}
        data = None
        for attempt in range(2):
            try:
                resp = requests.get(url, params=params, headers=_SINA_HEADERS, timeout=8)
                resp.raise_for_status()
                data = resp.json()
                break
            except Exception as exc:
                if attempt == 1:
                    logger.warning("Sina main flow failed after retry: %s", exc)
                    return []

        if not isinstance(data, list) or not data:
            return []

        results = []
        for item in data[:limit]:
            symbol = str(item.get("symbol", ""))
            if symbol.startswith(("sh", "sz")):
                symbol = symbol[2:]
            net = float(item.get("netamount", 0)) / 1e8
            results.append(StockMainFlow(
                symbol=symbol,
                name=str(item.get("name", "")),
                main_net_inflow=net,
                main_pct=0.0,
            ))
        return results

    def get_sector_flow(self, category: str = "industry", limit: int = 10) -> list[SectorFlow]:
        """Get sector/concept fund flow ranking via Sina MoneyFlow API.

        Args:
            category: "industry" (fenlei=0) or "concept" (fenlei=1)
        """
        fenlei = "0" if category == "industry" else "1"
        url = "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/MoneyFlow.ssl_bkzj_bk"
        params = {"page": "1", "num": str(limit), "sort": "netamount", "asc": "0", "fenlei": fenlei}
        data = None
        for attempt in range(2):
            try:
                resp = requests.get(url, params=params, headers=_SINA_HEADERS, timeout=8)
                resp.raise_for_status()
                data = resp.json()
                break
            except Exception as exc:
                if attempt == 1:
                    logger.warning("Sina sector flow (%s) failed after retry: %s", category, exc)
                    return []

        if not isinstance(data, list) or not data:
            return []

        results = []
        for item in data[:limit]:
            change_ratio = float(item.get("avg_changeratio", 0)) * 100
            net = float(item.get("netamount", 0)) / 1e8
            results.append(SectorFlow(
                sector_name=str(item.get("name", "")),
                change_pct=round(change_ratio, 2),
                net_inflow=round(net, 2),
            ))
        return results


def tool_get_fund_flow(category: str = "summary", limit: int = 10) -> dict[str, Any]:
    """Agent-callable tool: get fund flow data.

    Categories: "main_force", "industry", "concept", "summary" (all supported categories)
    """
    if category not in _FUND_FLOW_CATEGORIES:
        return {
            "error": f"Unknown category '{category}'. Use: main_force, industry, concept, summary.",
        }

    provider = FundFlowProvider()
    result: dict[str, Any] = {}
    unavailable: list[str] = []

    if category in ("main_force", "summary"):
        stocks = provider.get_stock_main_flow(limit=limit)
        if stocks:
            result["main_force_top"] = [
                {"symbol": s.symbol, "name": s.name, "net_inflow_yi": s.main_net_inflow}
                for s in stocks
            ]
        else:
            unavailable.append("main_force")

    if category in ("industry", "summary"):
        sectors = provider.get_sector_flow("industry", limit=limit)
        if sectors:
            result["industry_flow"] = [
                {"name": s.sector_name, "pct": s.change_pct, "net_inflow_yi": s.net_inflow}
                for s in sectors
            ]
        else:
            unavailable.append("industry")

    if category in ("concept", "summary"):
        concepts = provider.get_sector_flow("concept", limit=limit)
        if concepts:
            result["concept_flow"] = [
                {"name": s.sector_name, "pct": s.change_pct, "net_inflow_yi": s.net_inflow}
                for s in concepts
            ]
        else:
            unavailable.append("concept")

    if unavailable:
        result["unavailable"] = unavailable
        if not _is_cn_trading_hours():
            result["note"] = "当前为非交易时段，部分实时数据可能不可用。资金流向数据反映最近交易日情况。"

    return result
