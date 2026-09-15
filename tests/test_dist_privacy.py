from pathlib import Path

import pytest

from scripts.check_dist import _validate_names


@pytest.mark.parametrize("prefix", ["", "trade_compass_agent-0.2.2/"])
@pytest.mark.parametrize("name", [
    "data/paper_trades.jsonl",
    "memory_vault/knowledge.md",
    "temp/nested/artifact",
    "docs/market-readiness-2026-09-13.md",
    "docs/tushare-paid-verification-2026-09-12.json",
    "docs/personal-data-source-selection-2026-09-12.md.before-paid-verification",
])
def test_distribution_rejects_local_state_and_diagnostic_reports(prefix, name):
    with pytest.raises(ValueError, match="forbidden local"):
        _validate_names(Path("release"), {prefix + name})


def test_distribution_keeps_product_data_modules_and_public_documentation():
    _validate_names(Path("release"), {
        "trade_compass_agent/data/fundamentals.py",
        "trade_compass_agent-0.2.2/src/trade_compass_agent/data/providers.py",
        "trade_compass_agent-0.2.2/docs/configuration.md",
        "trade_compass_agent-0.2.2/docs/autonomous-paper-trading.md",
    })
