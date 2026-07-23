from __future__ import annotations

from pathlib import Path


class MemoryVault:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.ensure()

    def ensure(self) -> None:
        for name in [
            "daily_reviews",
            "weekly_summaries",
            "instruments",
            "rules",
        ]:
            (self.root / name).mkdir(parents=True, exist_ok=True)
        index = self.root / "INDEX.md"
        if not index.exists():
            index.write_text("# Trade Compass Memory Vault\n\nLocal-first trading memory.\n", encoding="utf-8")
