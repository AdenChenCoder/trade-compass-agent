"""Shared public industry/concept rankings with bounded upstream requests."""
from __future__ import annotations

import math

from .network import rate_limit_domain

_EM_HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://www.eastmoney.com"}
_EM_BOARD_SPECS: dict[str, tuple[str, str]] = {
    "concept": ("https://79.push2.eastmoney.com/api/qt/clist/get", "m:90 t:3 f:!50"),
    "industry": ("https://17.push2.eastmoney.com/api/qt/clist/get", "m:90 t:2 f:!50"),
}

_SINA_HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://vip.stock.finance.sina.com.cn"}
_SINA_BOARD_URL = "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/MoneyFlow.ssl_bkzj_bk"

def _safe_float(value, default=0.0):
    try:
        number = float(value)
        return number if math.isfinite(number) else default
    except (TypeError, ValueError):
        return default


def fetch_eastmoney_board_rows(*, board_type: str, limit: int) -> list[dict]:
    import requests

    url, fs = _EM_BOARD_SPECS[board_type]
    params = {
        "pn": "1",
        "pz": str(max(1, min(limit, 100))),
        "po": "1",
        "np": "1",
        "ut": "bd1d9ddb04089700cf9c27f6f7426281",
        "fltt": "2",
        "invt": "2",
        "fid": "f3",
        "fs": fs,
        "fields": "f2,f3,f8,f12,f14,f104,f105,f128,f136",
    }
    rate_limit_domain(url)
    resp = requests.get(url, params=params, headers=_EM_HEADERS, timeout=(1.0, 1.5))
    resp.raise_for_status()
    diff = (resp.json().get("data") or {}).get("diff") or []
    return [row for row in diff if isinstance(row, dict) and isinstance(row.get("f14"), str)
            and row["f14"].strip() and _safe_float(row.get("f3"), None) is not None] if isinstance(diff, list) else []


def fetch_sina_board_rows(*, board_type: str, limit: int) -> list[dict]:
    import requests

    fenlei = "0" if board_type == "industry" else "1"
    params = {
        "page": "1",
        "num": str(max(1, min(limit, 100))),
        "sort": "avg_changeratio",
        "asc": "0",
        "fenlei": fenlei,
    }
    resp = requests.get(_SINA_BOARD_URL, params=params, headers=_SINA_HEADERS, timeout=2.5)
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, list):
        return []

    rows: list[dict] = []
    for item in data[:limit]:
        change = _safe_float(item.get("avg_changeratio"), None)
        if not item.get("name") or change is None:
            continue
        rows.append({
            "f14": item.get("name", ""),
            "f3": change * 100,
            "f128": item.get("ts_name", ""),
            # Sina's board page does not expose a documented turnover percentage.
            # Its raw turnover must not be interpreted as Eastmoney f8 (%).
            "f8": None,
            "f104": None,
            "f105": None,
        })
    return rows
