from __future__ import annotations

import json
import os
import tempfile
import threading
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path


@dataclass
class CooldownState:
    consecutive_losses: int = 0
    cooling: bool = False
    updated_at: str = ""


class CooldownTracker:
    _lock = threading.Lock()

    def __init__(self, path: Path, threshold: int = 3) -> None:
        self.path = path
        self.threshold = threshold
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.state = self._load()

    def is_active(self) -> bool:
        return self.state.consecutive_losses >= self.threshold

    def record_loss(self) -> CooldownState:
        with self._lock:
            self.state = self._load()
            self.state.consecutive_losses += 1
            if self.state.consecutive_losses >= self.threshold:
                self.state.cooling = True
            self._save()
            return self.state

    def record_win(self) -> CooldownState:
        with self._lock:
            self.state = self._load()
            self.state.consecutive_losses = 0
            self.state.cooling = False
            self._save()
            return self.state

    def reset(self) -> CooldownState:
        with self._lock:
            self.state = CooldownState(updated_at=datetime.now().isoformat())
            self._save()
            return self.state

    def _load(self) -> CooldownState:
        if not self.path.exists():
            return CooldownState(updated_at=datetime.now().isoformat())
        try:
            text = self.path.read_text(encoding="utf-8").strip()
            if not text:
                return CooldownState(updated_at=datetime.now().isoformat())
            raw = json.loads(text)
            return CooldownState(
                consecutive_losses=int(raw.get("consecutive_losses", 0)),
                cooling=bool(raw.get("cooling", False)),
                updated_at=str(raw.get("updated_at", "")),
            )
        except (json.JSONDecodeError, ValueError, TypeError, OSError):
            return CooldownState(updated_at=datetime.now().isoformat())

    def _save(self) -> None:
        self.state.updated_at = datetime.now().isoformat()
        data = json.dumps(
            {
                "consecutive_losses": self.state.consecutive_losses,
                "cooling": self.state.cooling,
                "updated_at": self.state.updated_at,
                "date": date.today().isoformat(),
            },
            ensure_ascii=False,
            indent=2,
        ) + "\n"
        fd, tmp_path = tempfile.mkstemp(
            dir=str(self.path.parent), suffix=".tmp"
        )
        try:
            os.write(fd, data.encode("utf-8"))
            os.close(fd)
            fd = -1
            os.replace(tmp_path, str(self.path))
        except Exception:
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
            raise
