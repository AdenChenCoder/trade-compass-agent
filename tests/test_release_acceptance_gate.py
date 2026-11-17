"""Release evidence must reject missing coverage and falsely 'complete' reports."""
from copy import deepcopy
import json
import os
from pathlib import Path
import runpy
from unittest.mock import patch
import zipfile

import pytest


ROOT = Path(__file__).resolve().parents[1]
CHECKS = runpy.run_path(str(ROOT / "scripts/release_acceptance.py"))
MODEL = runpy.run_path(str(ROOT / "scripts/release_model_acceptance.py"))
NARRATIVE = runpy.run_path(str(ROOT / 'scripts/report_narrative_acceptance.py'))


def test_gap_controls_preserve_the_same_observation_and_sources():
    cases = NARRATIVE['SCOPE_CASES']
    names = ['bound_genuine_gap', 'bound_duplicate_gap', 'bound_outside_task_gap']
    rows = [cases[name] for name in names]
    assert len({row[0] for row in rows}) == 1
    assert all(row[1] is True and row[2] == rows[0][2] for row in rows)
    assert rows[-1][3] == '说明002415在2025年1月1日的收盘价，不预测未来。'
    assert rows[0][3] != rows[1][3] != rows[2][3]


def test_rule_controls_preserve_identical_body_and_missing_zero_one_many_sources():
    names = ['empty_rule_result', 'missing_rule_result_as_empty',
        'one_rule_alert_as_empty', 'multiple_rule_alerts_as_empty']
    cases = [NARRATIVE['SCOPE_CASES'][name] for name in names]
    assert len({row[0] for row in cases}) == len({row[3] for row in cases}) == 1
    assert [row[1] for row in cases] == [True, False, False, False]
    data = [json.loads(row[2][0][1])['data'] for row in cases]
    assert data == [{'alerts': []}, {}, {'alerts': ['条件A触发']}, {'alerts': ['条件A触发', '条件B触发']}]
    from trade_compass_agent.runtime.report_contract import EvidenceCatalog
    from trade_compass_agent.runtime.report_transport import review_receipts
    for name, row in zip(names, cases, strict=True):
        catalog = EvidenceCatalog.build(row[2], run_scope=name)
        assert len(catalog.facts) == 1
        quote = next(iter(catalog.facts.values()))
        assert quote.kind == 'quotation' and quote.value == row[2][0][1]
        assert [(r['tool'], r['result']) for r in review_receipts(catalog, [])] == row[2]


def test_profile_controls_change_evidence_without_changing_the_reader_claim():
    cases = NARRATIVE['SCOPE_CASES']
    missing = cases['profile_property_in_plan_missing']
    supplied = cases['profile_property_in_plan_supported']
    assert missing[0] == supplied[0] and missing[3] == supplied[3]
    assert missing[1] is False and supplied[1] is True
    assert supplied[2][:-1] == missing[2] and supplied[2][-1][0] == 'fetch_url'
    estimated = cases['profile_property_in_plan_estimated']
    assert estimated[0] == missing[0] and estimated[3] == missing[3] and estimated[1] is True
    assert estimated[2][0] == missing[2][0] and estimated[2][1][0] == 'get_chip_distribution'
    assert json.loads(estimated[2][1][1])['cost_90_high'] == 23
    negative = cases['estimate_as_measured_profile']
    positive = cases['estimated_pressure_plan']
    assert negative[2] == positive[2] and not negative[1] and positive[1]
    from trade_compass_agent.runtime.report_contract import EvidenceCatalog
    evidence = EvidenceCatalog.build(positive[2], run_scope='chip-fixture')
    upper = next(f for f in evidence.facts.values() if f.metric == 'cost_90_high')
    assert upper.value == '23' and upper.unit == '元/股' and upper.kind == 'chip_cost'
    assert upper.observed_at == '2025-01-02'
    assert json.loads(upper.scope)['method'] == 'eastmoney_cyq_algorithm'


def test_source_method_controls_preserve_the_claim_value_and_date_in_both_directions():
    from trade_compass_agent.runtime.report_contract import EvidenceCatalog
    cases = NARRATIVE['SCOPE_CASES']
    estimated = cases['estimate_method_supported']
    measured = cases['measured_profile_as_estimate']
    assert estimated[0] == measured[0] and estimated[3] == measured[3]
    assert estimated[1] is True and measured[1] is False
    catalogs = [EvidenceCatalog.build(row[2], run_scope='same-method-claim') for row in [estimated, measured]]
    for catalog in catalogs:
        high = next(f for f in catalog.facts.values() if f.source == 'get_bars' and f.metric == 'high')
        assert high.entity == '002415' and high.value == '23'
        assert high.observed_at == '2025-01-02' and high.unit == '元/股'
    assert any(f.kind == 'chip_cost' for f in catalogs[0].facts.values())
    assert not any(f.kind == 'chip_cost' for f in catalogs[1].facts.values())
    quote = next(f for f in catalogs[1].facts.values() if f.kind == 'quotation')
    assert quote.value == json.loads(measured[2][-1][1])['content']
    assert [(r['tool'], r['result']) for r in catalogs[1].receipts] == measured[2]
    assert cases['estimate_as_measured_profile'][2] == cases['estimated_pressure_plan'][2]
    quoted = cases['quoted_estimate_method_supported']
    assert quoted[0] == estimated[0] and quoted[3] == estimated[3] and quoted[1] is True
    quoted_catalog = EvidenceCatalog.build(quoted[2], run_scope='same-method-claim')
    assert quoted_catalog.source_semantics() == catalogs[1].source_semantics() == []
    assert next(f for f in quoted_catalog.facts.values() if f.kind == 'quotation').value == json.loads(quoted[2][-1][1])['content']


@pytest.mark.parametrize('selected,cases,passed', [
    (['normal'], [{'case': 'normal', 'passed': True}], True),
    ([], [], False),
    (['normal'], [], False),
    (['normal'], [{'case': 'other', 'passed': True}], False),
    (['normal'], [{'case': 'normal', 'passed': False}], False),
    (['normal'], [{'case': 'normal', 'passed': True}, {'case': 'normal', 'passed': True}], False),
])
def test_narrative_aggregation_requires_exact_completed_case_coverage(selected, cases, passed):
    original = {'cases': cases, 'passed': False}
    result = NARRATIVE['finalize_acceptance'](original, selected)
    assert result['passed'] is passed and original['passed'] is False


def test_task_coverage_controls_keep_evidence_and_factual_truth_independent_of_requested_outcome():
    controls = NARRATIVE['TASK_COVERAGE_CASES']
    cases = [NARRATIVE['SCOPE_CASES'][name] for name in controls]
    assert len(controls) == 5
    assert sum(expected for _, expected in controls.values()) == 2
    assert len({body for body, _, _, _ in cases}) == 1
    assert all(supported is True for _, supported, _, _ in cases)
    assert all(receipts == cases[0][2] for _, _, receipts, _ in cases)
    assert all(tool == 'get_bars' for tool, _ in cases[0][2])
    assert all(NARRATIVE['CASES'][name][1] is True for name in controls)
    assert controls['task_scope_action_goal'][1] is False


@pytest.mark.parametrize('supported,linked,time,available,current_scope,passed', [
    (True, True, 'entailed', True, True, True),
    (True, False, 'entailed', True, True, False),
    (False, False, 'unknown', True, True, True),
    (False, False, 'contradicted', True, True, True),
    (False, False, 'entailed', True, True, False),
    (False, True, 'entailed', True, True, True),
    (False, False, 'contradicted', False, True, False),
    (False, False, 'contradicted', True, False, False),
])
def test_qualitative_time_control_checks_completed_temporal_outcome(
    supported, linked, time, available, current_scope, passed,
):
    first = {'review_valid': available, 'factual_assessment_errors': {} if available else {'c0': {}},
        'temporal_checks': {'c0': {'scope': 'reference_date' if current_scope else 'other',
            'expected_date': '2025-01-02' if supported else '2025-01-03',
            'status': 'entailed' if supported else 'unknown',
            'evidence': [{'comparison_date': '2025-01-02'}] if linked else []}},
        'factual_review': {'assessments': [{'checks': {'time': time}}]}}
    assert NARRATIVE['qualitative_time_check_completed'](first, supported) is passed


@pytest.mark.parametrize('target_status,unrelated_status,dimension,available,passed', [
    ('contradicted', 'entailed', 'support', True, True),
    ('unknown', 'entailed', 'support', True, True),
    ('entailed', 'contradicted', 'support', True, False),
    ('contradicted', 'entailed', 'time', True, False),
    ('contradicted', 'entailed', 'support', False, False),
])
@pytest.mark.parametrize('case,target_text', [
    ('thesis_judgment_overclaim', '我认为这个估值足以保证持有不会亏损。'),
    ('compound_actor_from_activity', '结合上述开盘价、收盘价及0.594倍量比，可以确定该日非主力出货。'),
    ('ma_order_mixed_as_bullish', '002415在2025-01-02的均线呈多头排列。'),
    ('ma_order_bullish_denied', '002415在2025-01-02的均线尚未构成多头排列。'),
    ('missing_rule_result_as_empty', '所给规则扫描没有退出预警。'),
    ('profile_property_in_plan_missing', '上方23元/股为前期成交密集区，突破仍需观察成交量。'),
    ('estimate_as_measured_profile', '002415在2025-01-02的逐价成交量实测证实，23元附近是该日成交最密集的价位。'),
    ('measured_profile_as_estimate', '002415在2025-01-02的23元附近这一参考位，依据是筹码算法估算。'),
])
def test_thesis_negative_requires_its_own_completed_inferential_rejection(
    target_status, unrelated_status, dimension, available, passed, case, target_text,
):
    first = {'review_valid': True, 'factual_assessment_errors': {} if available else {'c1': {}},
        'factual_review_input': {'statements': [
            {'id': 'c0', 'context_id': 'p0', 'text': '按2025年1月2日的数据，600928市净率为0.8倍。'},
            {'id': 'c1', 'context_id': 'p0', 'text': target_text}]},
        'factual_review': {'assessments': [
            {'id': 'c0', 'checks': {'time': unrelated_status, 'support': 'entailed'}},
            {'id': 'c1', 'checks': {'time': 'entailed', 'support': 'entailed', dimension: target_status}}]}}
    result = NARRATIVE['target_rejection_check'](first, case)
    assert result['passed'] is passed and result['target_assertion_ids'] == ['c1']


@pytest.mark.parametrize('case', ['one_rule_alert_as_empty', 'multiple_rule_alerts_as_empty'])
@pytest.mark.parametrize('status,rejected', [('entailed', False), ('unknown', False), ('contradicted', True)])
@pytest.mark.parametrize('dimension,applicable', [('support', True), ('quantity', True), ('time', False), ('identity_scope', False)])
def test_nonempty_alert_result_requires_completed_contradiction(case, status, rejected, dimension, applicable):
    first = {'review_valid': True, 'factual_assessment_errors': {},
        'factual_review_input': {'statements': [
            {'id': 'c0', 'context_id': 'p0', 'text': '所给规则扫描没有退出预警。'}]},
        'factual_review': {'assessments': [{'id': 'c0', 'checks': {**dict.fromkeys(
            ('identity_scope', 'time', 'quantity', 'support'), 'entailed'), dimension: status}}]}}
    assert NARRATIVE['target_rejection_check'](first, case)['passed'] is (rejected and applicable)


def test_thesis_numeric_negative_requires_a_contradiction_at_the_known_quantity():
    first = {'review_valid': True, 'factual_assessment_errors': {},
        'factual_review_input': {'statements': [
            {'id': 'c0', 'context_id': 'p0', 'text': '按2025年1月2日的数据，600928市净率为'},
            {'id': 'c1', 'context_id': 'p0', 'text': '0.8倍。'}]},
        'factual_review': {'assessments': [
            {'id': 'c0', 'checks': {'quantity': 'entailed'}},
            {'id': 'c1', 'checks': {'quantity': 'contradicted'}}]}}
    result = NARRATIVE['target_rejection_check'](first, 'thesis_judgment_false_premise')
    assert result['passed'] and result['target_assertion_ids'] == ['c0', 'c1']
    first['factual_review']['assessments'][1]['checks']['quantity'] = 'unknown'
    assert not NARRATIVE['target_rejection_check'](first, 'thesis_judgment_false_premise')['passed']
    first['factual_review']['assessments'].pop()
    assert not NARRATIVE['target_rejection_check'](first, 'thesis_judgment_false_premise')['passed']


@pytest.mark.parametrize('selected', [['natural'], ['natural', 'reordered']])
def test_narrative_cli_finishes_after_real_source_selector_protocol(tmp_path, monkeypatch, selected):
    from trade_compass_agent.llm import providers
    output = tmp_path/'result'
    wheel = tmp_path/'explicit-double.whl'
    wheel.touch()
    monkeypatch.setattr('sys.argv', ['acceptance', '--wheel', str(wheel), '--output', str(output),
        '--model', 'explicit-double', '--fact-model', 'explicit-double', '--cases', *selected])
    monkeypatch.chdir(tmp_path)
    real_run_path = runpy.run_path
    monkeypatch.setattr(runpy, 'run_path', lambda path, *args, **kwargs:
        {'verify_installed_wheel': lambda _: {'wheel_sha256': 'explicit-double'}}
        if Path(path).name == 'release_acceptance.py' else real_run_path(path, *args, **kwargs))
    class Coverage:
        def stream_complete(self, messages, **kwargs):
            return providers.ChatCompletion(content='{"reviews":[],"coverage":{"complete":true,"missing":[]}}')
    def checker(system, user):
        payload = json.loads(user)
        if 'paragraphs' in payload:
            return json.dumps({'quantity_issues': [], 'paragraphs': [{'id': row['id'], 'parts': [{
                'through': row['source_units'][-1]['id'], 'kind': 'other', 'assertion_kind': 'observation',
                'time_scope': 'other', 'outcome': 'none', 'extent': 'none'}]} for row in payload['paragraphs']]})
        return json.dumps({'assessments': [{'id': row['id'], 'comparison': 'Explicit model double.',
            'support_basis': 'direct',
            'commitments': {'participant_activity': False, 'causal_relation': False},
            'time_evidence': [], 'verdict': 'entailed', 'checks': {key: 'entailed'
                for key in ('identity_scope', 'time', 'quantity', 'support')}} for row in payload['statements']]})
    monkeypatch.setattr(providers, 'create_chat_client', lambda _: Coverage())
    monkeypatch.setattr(providers, 'create_local_report_fact_check', lambda _: checker)
    with patch.dict(os.environ, dict(os.environ), clear=True), pytest.raises(SystemExit) as exit_info:
        NARRATIVE['main']()
    assert exit_info.value.code == 0
    result = json.loads((output/'acceptance.json').read_text())
    assert result['passed'] and not result['not_run']
    assert result['selected_cases'] == selected
    assert [row['case'] for row in result['cases']] == selected
    assert all(row['body_selector_calls'] == 1 for row in result['cases'])


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


def test_semantic_fixture_names_never_silently_replace_existing_scope_cases():
    assert not set(NARRATIVE['SCOPE_CASES']).intersection(NARRATIVE['TIME_CASES'])
    for name, (prose, expected, _, _) in NARRATIVE['SCOPE_CASES'].items():
        assert NARRATIVE['CASES'][name] == (prose, expected)
    for name in ['ma_order_mixed_as_bullish', 'ma_order_bullish_supported',
                 'ma_order_mixed_not_bullish', 'ma_order_bullish_denied']:
        assert name in NARRATIVE['CASES']
