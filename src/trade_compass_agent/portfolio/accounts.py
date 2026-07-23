"""Dynamic portfolio account store with CRUD operations."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from trade_compass_agent.domain import AccountKind


@dataclass
class Account:
    id: str
    kind: AccountKind
    name: str
    description: str
    capital: float
    created_at: str


_DEFAULT_ACCOUNTS: list[dict] = [
    {
        "id": "short_stock",
        "kind": "short_stock",
        "name": "短线股票",
        "description": "短线交易策略账户",
        "capital": 300_000,
    },
    {
        "id": "etf_rotation",
        "kind": "etf_rotation",
        "name": "ETF 轮动",
        "description": "ETF 轮动策略账户",
        "capital": 300_000,
    },
    {
        "id": "mid_term",
        "kind": "mid_term",
        "name": "中长线账户",
        "description": "中长线持仓账户",
        "capital": 400_000,
    },
]


class AccountStore:
    """CRUD for portfolio accounts. Storage: data/accounts.json"""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if not self._path.exists():
            self._seed_defaults()

    def _seed_defaults(self) -> None:
        now = datetime.now(timezone.utc).isoformat()
        accounts = []
        for d in _DEFAULT_ACCOUNTS:
            accounts.append({**d, "created_at": now})
        self._save(accounts)

    def _load(self) -> list[dict]:
        if not self._path.exists():
            return []
        return json.loads(self._path.read_text(encoding="utf-8"))

    def _save(self, data: list[dict]) -> None:
        self._path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def list(self) -> list[Account]:
        return [self._dict_to_account(d) for d in self._load()]

    def get(self, account_id: str) -> Account | None:
        for d in self._load():
            if d["id"] == account_id:
                return self._dict_to_account(d)
        return None

    def create(
        self,
        kind: AccountKind | str,
        name: str,
        description: str = "",
        capital: float = 0.0,
    ) -> Account:
        data = self._load()
        account_id = str(uuid.uuid4())[:8]
        now = datetime.now(timezone.utc).isoformat()
        kind_value = kind.value if isinstance(kind, AccountKind) else kind
        entry = {
            "id": account_id,
            "kind": kind_value,
            "name": name,
            "description": description,
            "capital": capital,
            "created_at": now,
        }
        data.append(entry)
        self._save(data)
        return self._dict_to_account(entry)

    def update(self, account_id: str, **kwargs) -> Account | None:
        data = self._load()
        for i, d in enumerate(data):
            if d["id"] == account_id:
                for k, v in kwargs.items():
                    if k in ("name", "description", "capital", "kind"):
                        if k == "kind" and isinstance(v, AccountKind):
                            v = v.value
                        d[k] = v
                data[i] = d
                self._save(data)
                return self._dict_to_account(d)
        return None

    def delete(self, account_id: str) -> bool:
        data = self._load()
        original_len = len(data)
        data = [d for d in data if d["id"] != account_id]
        if len(data) == original_len:
            return False
        self._save(data)
        return True

    @staticmethod
    def _dict_to_account(d: dict) -> Account:
        return Account(
            id=d["id"],
            kind=AccountKind(d["kind"]),
            name=d["name"],
            description=d.get("description", ""),
            capital=float(d.get("capital", 0)),
            created_at=d.get("created_at", ""),
        )
