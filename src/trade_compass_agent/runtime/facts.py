from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class FactStore:
    root: Path

    def append(self, fact: dict[str, Any], *, as_of: date | None = None) -> str:
        day = as_of or date.today()
        fact_id = fact.get("fact_id") or _stable_id(fact)
        record = {"fact_id": fact_id, **fact}
        path = self.root / f"{day.isoformat()}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        existing = self._ids(path)
        if fact_id not in existing:
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        return fact_id

    def list_day(self, day: date) -> list[dict[str, Any]]:
        path = self.root / f"{day.isoformat()}.jsonl"
        if not path.is_file():
            return []
        rows: list[dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
        return rows

    def query(
        self,
        *,
        symbol: str | None = None,
        workflow_id: str | None = None,
        run_id: str | None = None,
        start: date | None = None,
        end: date | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        end_day = end or date.today()
        start_day = start or end_day
        rows: list[dict[str, Any]] = []
        for day in _days(start_day, end_day):
            for row in self.list_day(day):
                if symbol and symbol not in {str(x) for x in row.get("symbols", [])}:
                    continue
                if workflow_id and row.get("workflow_id") != workflow_id:
                    continue
                if run_id and row.get("run_id") != run_id:
                    continue
                rows.append(row)
        return rows[-limit:]

    def _ids(self, path: Path) -> set[str]:
        if not path.is_file():
            return set()
        ids: set[str] = set()
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                ids.add(str(json.loads(line).get("fact_id", "")))
            except json.JSONDecodeError:
                continue
        return ids


def _stable_id(fact: dict[str, Any]) -> str:
    payload = json.dumps(fact, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def _days(start: date, end: date) -> list[date]:
    if start > end:
        start, end = end, start
    total = (end - start).days
    return [start + timedelta(days=offset) for offset in range(total + 1)]
