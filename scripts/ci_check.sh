#!/usr/bin/env bash
set -euo pipefail

if [ -z "${PYTHON:-}" ]; then
  if [ -x .venv/bin/python ]; then
    PYTHON=".venv/bin/python"
  else
    PYTHON="python"
  fi
fi

"$PYTHON" scripts/check_assets.py
"$PYTHON" -m ruff check \
  scripts/check_assets.py \
  scripts/check_dist.py \
  tests/test_workflow_governance.py \
  tests/test_fetch_url_tool.py \
  tests/test_install_script.py \
  tests/test_specialists.py \
  src/trade_compass_agent/runtime/workflows \
  src/trade_compass_agent/runtime/readers \
  src/trade_compass_agent/runtime/facts.py \
  src/trade_compass_agent/runtime/schema_validator.py \
  src/trade_compass_agent/runtime/specialists/asset_runner.py \
  src/trade_compass_agent/runtime/specialists/assets.py \
  src/trade_compass_agent/runtime/specialists/multi_agent \
  src/trade_compass_agent/runtime/specialists/run.py \
  src/trade_compass_agent/runtime/tools/fetch_url.py \
  src/trade_compass_agent/runtime/tools/policy.py \
  src/trade_compass_agent/runtime/tools/readers.py \
  src/trade_compass_agent/runtime/tools/catalyst_calendar.py \
  src/trade_compass_agent/runtime/tools/idea_generation.py \
  src/trade_compass_agent/runtime/tools/artifact_tracking.py \
  src/trade_compass_agent/runtime/tools/builtin_operations.py \
  src/trade_compass_agent/ops/job_definition.py \
  src/trade_compass_agent/evaluation/workflow_artifacts.py \
  src/trade_compass_agent/web/api.py
MPLBACKEND=Agg "$PYTHON" -m pytest -q
