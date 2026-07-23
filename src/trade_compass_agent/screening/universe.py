"""Universe resolution — fetch full A-share stock list.

Parallel direct HTTP to SSE/SZSE APIs. No AKShare dependency (too slow —
sequential requests across 4 exchanges take ~40s; this does SH+SZ in ~5s).
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from io import BytesIO

import requests

logger = logging.getLogger(__name__)

_TIMEOUT = (5, 10)  # (connect_timeout, read_timeout)
_SSE_HEADERS = {
    "Host": "query.sse.com.cn",
    "Referer": "https://www.sse.com.cn/assortment/stock/list/share/",
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
    ),
}


@dataclass(frozen=True)
class StockInfo:
    symbol: str
    name: str
    market_cap_yi: float | None = None
    industry: str | None = None


def resolve_universe(boards: list[str] | None = None) -> list[StockInfo]:
    """Resolve A-share universe from SSE + SZSE in parallel."""
    stocks = _fetch_parallel(boards)
    if stocks:
        return stocks

    logger.warning("Failed to fetch universe from any provider")
    return []


def _fetch_parallel(boards: list[str] | None) -> list[StockInfo]:
    tasks = {
        "sh_main": lambda: _fetch_sse("1"),
        "sh_kcb": lambda: _fetch_sse("8"),
        "sz": lambda: _fetch_szse(),
    }
    all_stocks: list[StockInfo] = []
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {pool.submit(fn): name for name, fn in tasks.items()}
        for fut in as_completed(futures):
            name = futures[fut]
            try:
                result = fut.result()
                all_stocks.extend(result)
                logger.debug("Universe %s: %d stocks", name, len(result))
            except Exception as exc:
                logger.warning("Universe %s failed: %s", name, exc)

    if boards:
        all_stocks = [s for s in all_stocks if any(s.symbol.startswith(b) for b in boards)]

    logger.info("Universe resolved: %d stocks (parallel SSE+SZSE)", len(all_stocks))
    return all_stocks


def _fetch_sse(stock_type: str) -> list[StockInfo]:
    """Fetch from SSE query API (Shanghai main board or KeChuangBan)."""
    params = {
        "STOCK_TYPE": stock_type,
        "REG_PROVINCE": "",
        "CSRC_CODE": "",
        "STOCK_CODE": "",
        "sqlId": "COMMON_SSE_CP_GPJCTPZ_GPLB_GP_L",
        "COMPANY_STATUS": "2,4,5,7,8",
        "type": "inParams",
        "isPagination": "true",
        "pageHelp.cacheSize": "1",
        "pageHelp.beginPage": "1",
        "pageHelp.pageSize": "10000",
        "pageHelp.pageNo": "1",
        "pageHelp.endPage": "1",
    }
    last_exc: Exception | None = None
    for attempt in range(2):
        try:
            r = requests.get(
                "https://query.sse.com.cn/sseQuery/commonQuery.do",
                params=params,
                headers=_SSE_HEADERS,
                timeout=_TIMEOUT,
            )
            r.raise_for_status()
            data = r.json()
            results: list[StockInfo] = []
            for item in data.get("result", []):
                code = str(item.get("A_STOCK_CODE", "")).strip()
                name = str(item.get("SEC_NAME_CN", "")).strip()
                if code and name:
                    results.append(StockInfo(symbol=code, name=name))
            return results
        except Exception as exc:
            last_exc = exc
            if attempt == 0:
                logger.debug("SSE type=%s attempt %d failed: %s", stock_type, attempt, exc)
    raise last_exc  # type: ignore[misc]


def _fetch_szse() -> list[StockInfo]:
    """Fetch from SZSE xlsx API (Shenzhen A-shares)."""
    import pandas as pd

    params = {
        "SHOWTYPE": "xlsx",
        "CATALOGID": "1110",
        "TABKEY": "tab1",
        "random": "0.6935816432433362",
    }
    r = requests.get(
        "https://www.szse.cn/api/report/ShowReport",
        params=params,
        timeout=_TIMEOUT,
    )
    r.raise_for_status()
    df = pd.read_excel(BytesIO(r.content))
    results: list[StockInfo] = []
    for _, row in df.iterrows():
        code = str(row.get("A股代码", "")).split(".")[0].strip().zfill(6)
        name = str(row.get("A股简称", "")).strip()
        if code and name and code != "000nan":
            results.append(StockInfo(symbol=code, name=name))
    return results


def filter_st(stocks: list[StockInfo]) -> list[StockInfo]:
    """Remove ST/退市 stocks. Checks for *ST, ST prefix patterns."""
    return [s for s in stocks if not _is_st(s.name)]


def _is_st(name: str) -> bool:
    """Check if stock name indicates ST or delisting status."""
    upper = name.upper()
    if "退" in name:
        return True
    if upper.startswith("*ST") or upper.startswith("ST"):
        return True
    if " ST" in upper or "　ST" in upper:
        return True
    return False
