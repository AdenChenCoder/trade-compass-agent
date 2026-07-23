"""Instrument Pages — per-symbol structured knowledge (Tier 4 Archive).

Each tracked instrument gets a
persistent markdown page with sections for rationale, key levels, trade history,
and notes. The agent can recall and update these pages.

Storage: memory_vault/instruments/{symbol}.md
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

_SECTION_RE = re.compile(r"^##\s+(.+)$", re.MULTILINE)
_META_RE = re.compile(r"^\*最后更新:\s*(.+)\*$", re.MULTILINE)
_CREATED_AT_RE = re.compile(r"^\*创建时间:\s*(.+)\*$", re.MULTILINE)

TEMPLATE = """\
# {symbol} - {name}

## 关注理由


## 关键价位
- 支撑:
- 压力:

## 交易历史


## 笔记


---
*创建时间: {created_at}*
*最后更新: {date}*
"""


class InstrumentStore:
    """Manages per-symbol instrument pages under memory_vault/instruments/."""

    def __init__(self, memory_vault_dir: Path) -> None:
        self._dir = memory_vault_dir / "instruments"
        self._dir.mkdir(parents=True, exist_ok=True)

    def recall(self, symbol: str) -> str | None:
        """Read the full instrument page. Returns None if not found."""
        path = self._path_for(symbol)
        if not path.is_file():
            return None
        return path.read_text(encoding="utf-8")

    def update_section(self, symbol: str, section: str, content: str, name: str = "") -> dict:
        """Update a specific section of an instrument page.

        Creates the page from template if it doesn't exist.
        """
        path = self._path_for(symbol)
        if not path.is_file():
            self._create(symbol, name)

        page = path.read_text(encoding="utf-8")
        header = f"## {section}"

        if header not in page:
            return {"ok": False, "error": f"Section '{section}' not found. Available: {self._list_sections(page)}"}

        updated = self._replace_section(page, section, content)
        updated = self._update_timestamp(updated)
        self._atomic_write(path, updated)
        logger.info("Updated instrument page %s section '%s'", symbol, section)
        return {"ok": True, "symbol": symbol, "section": section}

    def append_trade(self, symbol: str, side: str, quantity: int, price: float, reason: str = "", name: str = "") -> dict:
        """Append a trade entry to the instrument's trade history."""
        path = self._path_for(symbol)
        if not path.is_file():
            self._create(symbol, name)

        page = path.read_text(encoding="utf-8")
        date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        entry = f"- {date} {side} {quantity}股 @{price}"
        if reason:
            entry += f" ({reason})"

        section_content = self._get_section_content(page, "交易历史")
        new_content = (section_content.rstrip() + "\n" + entry).strip()
        updated = self._replace_section(page, "交易历史", new_content)
        updated = self._update_timestamp(updated)
        self._atomic_write(path, updated)
        return {"ok": True, "entry": entry}

    def replace_trade_history(self, symbol: str, entries: list[str], name: str = "") -> dict:
        """Replace the trade history section with ledger-derived entries."""
        content = "\n".join(entries).strip()
        return self.update_section(symbol, "交易历史", content, name=name)

    def list_instruments(self) -> list[str]:
        """List all tracked instrument symbols."""
        return sorted(
            p.stem for p in self._dir.glob("*.md")
        )

    def exists(self, symbol: str) -> bool:
        return self._path_for(symbol).is_file()

    def created_at(self, symbol: str) -> str:
        """Return the persisted creation time, or blank for legacy pages."""
        page = self.recall(symbol)
        if page is None:
            return ""
        match = _CREATED_AT_RE.search(page)
        return match.group(1).strip() if match else ""

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _path_for(self, symbol: str) -> Path:
        safe = symbol.replace("/", "_").replace("\\", "_")
        return self._dir / f"{safe}.md"

    def _create(self, symbol: str, name: str = "") -> None:
        now = datetime.now(timezone.utc)
        content = TEMPLATE.format(
            symbol=symbol,
            name=name or symbol,
            created_at=now.isoformat(),
            date=now.strftime("%Y-%m-%d"),
        )
        self._atomic_write(self._path_for(symbol), content)
        logger.info("Created instrument page for %s", symbol)

    def _list_sections(self, page: str) -> list[str]:
        return _SECTION_RE.findall(page)

    def _get_section_content(self, page: str, section: str) -> str:
        """Extract content between a ## header and the next ## or ---."""
        pattern = re.compile(
            rf"^##\s+{re.escape(section)}\s*\n(.*?)(?=^##|\n---)",
            re.MULTILINE | re.DOTALL,
        )
        m = pattern.search(page)
        return m.group(1).strip() if m else ""

    def _replace_section(self, page: str, section: str, new_content: str) -> str:
        """Replace content of a section (between header and next section/---)."""
        pattern = re.compile(
            rf"(^##\s+{re.escape(section)}\s*\n)(.*?)(?=^##|\n---)",
            re.MULTILINE | re.DOTALL,
        )

        def _repl(m: re.Match) -> str:
            return m.group(1) + new_content + "\n\n"

        return pattern.sub(_repl, page, count=1)

    def _update_timestamp(self, page: str) -> str:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return _META_RE.sub(f"*最后更新: {now}*", page)

    def _atomic_write(self, path: Path, content: str) -> None:
        fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        try:
            os.write(fd, content.encode("utf-8"))
            os.close(fd)
            os.replace(tmp, path)
        except Exception:
            try:
                os.close(fd)
            except OSError:
                pass
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
