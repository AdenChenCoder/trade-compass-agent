"""Release evidence must reject missing coverage and falsely 'complete' reports."""
from copy import deepcopy
from pathlib import Path
import runpy
import zipfile

import pytest


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
                                       {"id": "change", "type": "calculation", "refs": ["0", "1"]},
                                       {"id": "answer", "type": "paragraph", "parts": [
                                           {"type": "value", "ref": "change", "field": "value_with_unit"}]}]},
                "review": {"reviews": [{"id": "answer", "supported": True}]},
                "published_text": detail["analysis"], "reader_body": detail["analysis"],
                "review_input": {"report": detail["analysis"]}}
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
    artifact["published_text"] = detail["analysis"]
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


def test_requested_calculation_must_be_visible_not_an_unrelated_result():
    detail, artifact, receipt = valid_report()
    artifact["calculations"]["other"] = {"operation": "change_pct", "refs": ["1", "0"], "result": "-9.090909"}
    artifact["document"]["items"][-1]["parts"][0]["ref"] = "other"
    with pytest.raises(RuntimeError, match="not the body"):
        MODEL["check_report"](detail, artifact, receipt, missing=False)
