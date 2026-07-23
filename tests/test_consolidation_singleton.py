from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import trade_compass_agent.memory.consolidation as consolidation_mod
from trade_compass_agent.memory.consolidation import shared_consolidation_worker


def test_shared_consolidation_worker_singleton(tmp_path: Path) -> None:
    consolidation_mod._singleton = None
    obs = MagicMock()
    sessions = MagicMock()
    memory = MagicMock()

    first = shared_consolidation_worker(obs, sessions, memory, llm_call=None)
    second = shared_consolidation_worker(obs, sessions, memory, llm_call=None)

    assert first is second
    consolidation_mod._singleton = None
