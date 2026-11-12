"""Release evidence must reject missing coverage and falsely 'complete' reports."""
from copy import deepcopy
from pathlib import Path
import os
import runpy
import subprocess
import zipfile

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
CHECKS = runpy.run_path(str(ROOT / "scripts/release_acceptance.py"))
MODEL = runpy.run_path(str(ROOT / "scripts/release_model_acceptance.py"))


@pytest.mark.parametrize("counts", [
    'tests="0"', 'tests="2" skipped="1"', 'tests="2" failures="1"',
    'tests="2" errors="1"',
])
def test_gate_rejects_empty_skipped_failed_or_broken_tests(tmp_path, counts):
    path = tmp_path / "junit.xml"
    path.write_text(f"<testsuites><testsuite {counts}/></testsuites>")
    with pytest.raises(RuntimeError):
        CHECKS["check_junit"](path)


def test_gate_accepts_complete_tests(tmp_path):
    path = tmp_path / "junit.xml"
    path.write_text('<testsuites><testsuite tests="2"/><testsuite tests="3"/></testsuites>')
    assert CHECKS["check_junit"](path)["tests"] == 5


def test_gate_rejects_a_different_installed_artifact_and_source_import(tmp_path, monkeypatch):
    import trade_compass_agent
    import trade_compass_agent.config as config
    package = tmp_path/"trade_compass_agent"
    package.mkdir()
    init = package/"__init__.py"
    init.write_text("original")
    wheel = tmp_path/"candidate.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("trade_compass_agent/__init__.py", "original")
    monkeypatch.setattr(trade_compass_agent, "__file__", str(init))
    monkeypatch.setattr(config, "is_source_checkout", lambda: False)
    assert CHECKS["verify_installed_wheel"](wheel)["verified_files"] == 1
    init.write_text("different")
    with pytest.raises(RuntimeError, match="differs"):
        CHECKS["verify_installed_wheel"](wheel)
    monkeypatch.setattr(config, "is_source_checkout", lambda: True)
    with pytest.raises(RuntimeError, match="outside the checkout"):
        CHECKS["verify_installed_wheel"](wheel)


def valid_report():
    receipt = {"bars": [{"timestamp": "2025-01-01", "close": 20},
                        {"timestamp": "2025-01-02", "close": 22}]}
    detail = {"status": "completed", "analysis": "2025-01-01 20; 2025-01-02 22; 研判：价格上升",
              "step_runs": [{"data": {"verification": {"execution": "completed",
                   "report": "complete", "findings": []}}}]}
    artifact = {"facts": [{"id": str(i), "subject": "002415", "metric": "close",
                            "value": str(b["close"]), "observed_at": b["timestamp"]}
                           for i, b in enumerate(receipt["bars"])],
                "calculations": {"change": {"operation": "change_pct", "refs": ["0", "1"], "result": "10"}},
                "document": {"items": [{"id": "observation", "type": "fact", "ref": "0"},
                                       {"id": "change", "type": "calculation", "refs": ["0", "1"]}]}}
    return detail, artifact, receipt


@pytest.mark.parametrize("fault", ["high", "symbol", "date", "arithmetic", "no_calculation", "partial"])
def test_model_complete_label_cannot_override_business_oracle(fault):
    detail, artifact, receipt = valid_report()
    MODEL["check_report"](detail, artifact, receipt, missing=False)
    artifact = deepcopy(artifact)
    if fault in {"high", "symbol", "date"}:
        key, value = {"high": ("metric", "high"), "symbol": ("subject", "000001"),
                      "date": ("observed_at", "2024-01-01")}[fault]
        artifact["facts"][0][key] = value
    elif fault == "arithmetic":
        artifact["calculations"]["change"]["result"] = "11"
    elif fault == "no_calculation":
        artifact["calculations"] = {}
    else:
        detail["status"] = "degraded"
    with pytest.raises(RuntimeError):
        MODEL["check_report"](detail, artifact, receipt, missing=False)


def test_real_gap_is_required_but_cannot_excuse_wrong_calculation():
    detail, artifact, receipt = valid_report()
    with pytest.raises(RuntimeError):
        MODEL["check_report"](detail, artifact, receipt, missing=True)

    detail["status"] = "degraded"
    detail["analysis"] += " 缺少营收依据"
    verification = detail["step_runs"][0]["data"]["verification"]
    verification.update(report="partial", findings=[{"code": "task_incomplete"}])
    MODEL["check_report"](detail, artifact, receipt, missing=True)
    artifact["facts"][0]["metric"] = "high"
    with pytest.raises(RuntimeError):
        MODEL["check_report"](detail, artifact, receipt, missing=True)


def test_correct_requested_calculation_cannot_hide_an_extra_foreign_observation():
    detail, artifact, receipt = valid_report()
    artifact["facts"].append({**artifact["facts"][0], "id": "foreign", "subject": "000005"})
    artifact["document"]["items"].append({"id": "extra", "type": "fact", "ref": "foreign"})
    with pytest.raises(RuntimeError, match="another instrument"):
        MODEL["check_report"](detail, artifact, receipt, missing=False)

@pytest.mark.parametrize("filename,build_job", [
    ("release.yml", "build"), ("test-publish.yml", "verify-and-build"), ("ci.yml", "wheel-smoke"),
])
def test_installed_acceptance_is_mandatory_before_publish(filename, build_job):
    jobs = yaml.safe_load((ROOT/".github/workflows"/filename).read_text())["jobs"]
    steps = jobs[build_job]["steps"]
    gate = next(s for s in steps if "scripts/release_acceptance.py" in s.get("run", ""))
    assert "if" not in gate and not gate.get("continue-on-error")
    assert "--wheel dist/*.whl" in gate["run"]
    if "publish" in jobs:
        needs = jobs["publish"]["needs"]
        assert build_job in ([needs] if isinstance(needs, str) else needs)
        assert "if" not in jobs["publish"]


def test_real_model_failure_or_missing_configuration_blocks_production_publish():
    jobs = yaml.safe_load((ROOT/".github/workflows/release.yml").read_text())["jobs"]
    assert "model-acceptance" in jobs["publish"]["needs"]
    gate = jobs["model-acceptance"]
    assert gate["needs"] == "build"
    assert "if" not in gate and not gate.get("continue-on-error")
    assert "ubuntu-latest" in gate["runs-on"]  # Fail fast when no runner was chosen.
    preflight = gate["steps"][0]
    for variable in ("RELEASE_MODEL_RUNNER", "RELEASE_MODEL_ID"):
        assert f'test -n "${variable}"' in preflight["run"]
    assert "exit 1" in preflight["run"]
    base_env = {k: v for k, v in os.environ.items() if not k.startswith("RELEASE_MODEL_")}
    for variables, expected in [({}, False), ({"RELEASE_MODEL_RUNNER": "isolated"}, False),
                                ({"RELEASE_MODEL_RUNNER": "isolated", "RELEASE_MODEL_ID": "required-model"}, True)]:
        result = subprocess.run(["bash", "-e", "-c", preflight["run"]],
                                env={**base_env, **variables}, capture_output=True, text=True)
        assert (result.returncode == 0) is expected
    model = next(s for s in gate["steps"] if "scripts/release_model_acceptance.py" in s.get("run", ""))
    assert "if" not in model and not model.get("continue-on-error")
