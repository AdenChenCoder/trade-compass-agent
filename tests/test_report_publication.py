import json
from report_contract_helpers import report_facts
from types import SimpleNamespace

import pytest

from trade_compass_agent.runtime.exceptions import TurnInterruptedError
from trade_compass_agent.runtime.report_publication import persist_publication, publish_report, upstream_evidence


RECEIPTS = [("get_bars", json.dumps({"symbol": "002415", "name": "海康威视", "bars": [
    {"timestamp": "2026-11-09", "close": 6.60}, {"timestamp": "2026-11-10", "close": 7.46}]}))]


@pytest.mark.parametrize("executed", [False, True])
def test_generation_schema_allows_empty_items_only_with_owned_execution(executed):
    receipts = [("place_paper_trade", json.dumps({"status": "executed", "trade_id": "owned",
        "symbol": "000001", "side": "buy", "quantity": 100, "price": 10, "account": "short_stock",
        "timestamp": "2026-11-10T10:00:00+08:00"}))] if executed else RECEIPTS
    def call(system, user):
        payload = json.loads(user)
        if "schema" in payload:
            from trade_compass_agent.llm.providers import _maintenance_response_schema
            assert _maintenance_response_schema.get() == payload["schema"]
            assert payload["schema"]["properties"]["items"].get("minItems", 0) == (0 if executed else 1)
            return '{"schema_version":1,"items":[]}'
        return '{"reviews":[],"coverage":{"complete":true,"missing":[]}}'
    result = publish_report(task="报告结果", draft="", tool_results=receipts, run_scope="empty", call=call)
    assert (result.verification["status"] == "complete") == executed
    assert ("owned" in result.text) == executed


@pytest.mark.parametrize("metric,unit,accepted", [("pnl_pct", "%", True), ("change_pct", "%", True),
                                                 ("pnl_pct", "元", False), ("quantity", "股", False)])
def test_percentage_plans_use_producer_types_without_accepting_wrong_units(metric, unit, accepted):
    receipts = [("analyze_portfolio", json.dumps({"positions": [
        {"symbol": "002415", "account": "short_stock", metric: -2.37}]}))]
    def call(system, user):
        payload = json.loads(user)
        if "inferences" in payload:
            return '{"reviews":[],"coverage":{"complete":true,"missing":[]}}'
        fact = next(f for f in report_facts(payload) if f["metric"] == metric)
        return json.dumps({"schema_version": 1, "items": [{"id": "plan", "section": "plan", "type": "condition",
            "ref": fact["id"], "comparison": "lt", "threshold": "-5", "unit": unit, "action": "review"}]})
    result = publish_report(task="设置持仓比例观察条件", draft="", tool_results=receipts, run_scope="plan", call=call)
    assert (result.verification["status"] == "complete") == accepted
    assert result.verification["execution"] == "completed"
    if accepted:
        assert "低于-5%" in result.text and "不是已发生事实或已执行指令" in result.text
    else:
        assert any(f["code"] == "invalid_evidence" for f in result.verification["findings"])


def compose(payload, *, inference=False):
    facts = [f for f in report_facts(payload) if f["metric"] == "close"]
    items = [{"id": "change", "type": "calculation", "section": "market", "operation": "change_pct",
              "refs": [f["id"] for f in facts]}]
    if inference:
        items.append({"id": "judgment", "type": "inference", "section": "assessment",
                      "text": "公司因业绩改善而上涨", "refs": [f["id"] for f in facts]})
    return {"schema_version": 1, "items": items}


def call_model(*, inference=False, supported=True, missing=(), invalid=False, unavailable=False):
    calls = []
    def call(system, user):
        payload = json.loads(user)
        calls.append(payload)
        if "schema" in payload:
            if invalid:
                return '{"schema_version":1,"items":[{"id":"invented"}]}'
            return json.dumps(compose(payload, inference=inference))
        if unavailable:
            raise TimeoutError("review unavailable")
        return json.dumps({"reviews": [{"id": "judgment", "supported": supported,
                                       "reason": "" if supported else "价格不能证明上涨原因"}] if inference else [],
                           "coverage": {"complete": not missing, "missing": list(missing)}})
    return call, calls


@pytest.mark.parametrize("draft", ["7.46反弹到6.60", "海康威视上涨13.03%", "一旦低于均线则观察", "所属金融行业净流出15.03亿"])
def test_draft_wording_cannot_change_bound_facts_or_calculations(draft):
    call, calls = call_model()
    result = publish_report(task="计算这两个交易日价格变动", draft=draft, tool_results=RECEIPTS, run_scope="a", call=call)
    assert result.verification["status"] == "complete"
    assert "13.03%" in result.text and "海康威视" in result.text
    assert "反弹到6.60" not in result.text
    assert len(calls) == 2


def test_unsupported_judgment_is_withheld_while_facts_remain_incomplete():
    call, _ = call_model(inference=True, supported=False)
    result = publish_report(task="分析价格和上涨原因", draft="", tool_results=RECEIPTS, run_scope="a", call=call)
    assert "13.03%" in result.text and "因业绩改善" not in result.text
    assert result.verification["status"] == "incomplete"
    assert result.verification["execution"] == "completed"
    assert result.verification["findings"][0]["code"] == "insufficient_support"


@pytest.mark.parametrize("mode,code", [("missing", "required_content_missing"), ("unavailable", "review_unavailable")])
def test_fact_only_response_requires_task_completeness(mode, code):
    call, _ = call_model(missing=["未评估持仓逻辑"] if mode == "missing" else [], unavailable=mode == "unavailable")
    result = publish_report(task="分析走势和持仓逻辑", draft="", tool_results=RECEIPTS, run_scope="a", call=call)
    assert result.verification["status"] == "incomplete"
    assert any(f["code"] == code for f in result.verification["findings"])
    assert "13.03%" in result.text


def test_format_failure_preserves_actual_execution_and_is_bounded():
    execution = {"status": "executed", "trade_id": "fill-1", "symbol": "002415", "account": "short_term",
                 "side": "sell", "price": 7.46, "quantity": 100, "timestamp": "2026-11-10T15:00:00"}
    call, calls = call_model(invalid=True)
    result = publish_report(task="执行后写报告", draft="unverified draft", tool_results=[*RECEIPTS, ("place_paper_trade", json.dumps(execution))],
                            run_scope="a", call=call)
    assert len(calls) == 3
    assert result.verification["report"] == "unavailable" and result.verification["execution"] == "completed"
    assert "已成交" in result.text and "fill-1" in result.text and "unverified draft" not in result.text
    assert result.artifact["execution_records"][0]["status"] == "executed"


def test_history_and_upstream_execution_are_not_current_fills():
    receipt = ("place_paper_trade", json.dumps({"status": "executed", "trade_id": "old"}))
    call, _ = call_model()
    result = publish_report(task="计算", draft="", tool_results=[*RECEIPTS, receipt], current_tool_results=RECEIPTS, run_scope="a", call=call)
    assert not result.artifact["execution_records"] and "已成交" not in result.text


@pytest.mark.parametrize("receipt,status", [({"trade_rejected": True, "error": "余额不足"}, "completed"),
                                          ({"error": "timeout", "execution_unconfirmed": True}, "unknown")])
def test_rejected_and_unknown_operations_have_distinct_outcomes(receipt, status):
    call, _ = call_model()
    result = publish_report(task="计算", draft="", tool_results=[*RECEIPTS, ("place_paper_trade", json.dumps(receipt))], run_scope="a", call=call)
    assert result.verification["execution"] == status
    assert (result.verification["status"] == "complete") == (status == "completed")


def test_cancellation_after_model_return_cannot_publish(tmp_path):
    cancelled = False
    def check():
        if cancelled:
            raise TurnInterruptedError()
    def call(*_):
        nonlocal cancelled
        cancelled = True
        return "{}"
    with pytest.raises(TurnInterruptedError):
        publish_report(task="x", draft="", tool_results=RECEIPTS, run_scope="a", call=call, check_active=check)
    assert not (tmp_path / "report_artifacts").exists()


def test_owned_artifacts_reopen_and_reject_cross_run_or_tampering(tmp_path):
    call, _ = call_model()
    result = publish_report(task="计算", draft="", tool_results=RECEIPTS, run_scope="session:turn", run_id="run-a", call=call)
    reference = persist_publication(tmp_path, result)
    upstream = {"first": SimpleNamespace(data={"verification": {**result.verification, **reference}})}
    assert upstream_evidence(tmp_path, upstream, "run-a") == (RECEIPTS, [])
    assert upstream_evidence(tmp_path, upstream, "run-b")[1][0].code == "upstream_evidence_unavailable"
    artifact = tmp_path / "report_artifacts" / f"{reference['artifact_id']}.json"
    artifact.write_text(artifact.read_text().replace("7.46", "9.99"))
    assert upstream_evidence(tmp_path, upstream, "run-a")[0] == []
    # An arbitrary model-created receipt array is never accepted.
    assert upstream_evidence(tmp_path, {"x": SimpleNamespace(data={"report_evidence": RECEIPTS})}, "run-a") == ([], [])


def test_cancel_before_artifact_commit_leaves_no_report(tmp_path):
    call, _ = call_model()
    result = publish_report(task="计算", draft="", tool_results=RECEIPTS, run_scope="a", call=call)
    calls = 0
    def check():
        nonlocal calls
        calls += 1
        if calls == 2:
            raise TurnInterruptedError()
    with pytest.raises(TurnInterruptedError):
        persist_publication(tmp_path, result, check_active=check)
    assert not list((tmp_path / "report_artifacts").iterdir())


@pytest.mark.parametrize("verified", [True, False])
def test_scheduled_memory_uses_the_same_contract_and_commits_only_verified_content(tmp_path, verified):
    from trade_compass_agent.llm.providers import ToolCall
    from trade_compass_agent.runtime.loop import _execute_tool_calls
    from trade_compass_agent.runtime.tools.memory import tool_write_memory
    saved, validations = [], []
    def validate(content, receipts):
        validations.append(content)
        call, _ = call_model(invalid=not verified)
        return publish_report(task="核验拟保存内容", draft=content, tool_results=receipts,
                              current_tool_results=[], run_scope="memory", call=call)
    class Tools:
        def execute(self, name, arguments):
            assert name == "write_memory"
            args = json.loads(arguments)
            saved.append(args["content"])
            return tool_write_memory(tmp_path, args["scope"], args["content"])
    result = _execute_tool_calls([ToolCall("save", "write_memory", json.dumps({
        "scope": "general", "content": "价格涨幅乱写99%"}))], Tools(), None, RECEIPTS, memory_report=validate)
    receipt = json.loads(result["save"])
    assert validations == ["价格涨幅乱写99%"]
    if verified:
        assert len(saved) == 1 and "13.03%" in saved[0] and "99%" not in saved[0]
        assert receipt["verification"]["status"] == "complete" and receipt["path"]
    else:
        assert not saved and receipt["fact_check_failed"]
        assert receipt["verification"]["report"] == "unavailable"


def test_scheduled_memory_cancelled_validation_never_calls_writer():
    from trade_compass_agent.llm.providers import ToolCall
    from trade_compass_agent.runtime.loop import _execute_tool_calls
    def stopped(*args):
        raise TurnInterruptedError()
    class Tools:
        def execute(self, *_):
            pytest.fail("cancelled memory must never reach the writer")
    with pytest.raises(TurnInterruptedError):
        _execute_tool_calls([ToolCall("save", "write_memory", '{"content":"待核验内容"}')],
                            Tools(), None, RECEIPTS, memory_report=stopped)


def test_nested_owned_artifacts_need_runtime_tree_and_exact_child_run(tmp_path):
    call, _ = call_model()
    result = publish_report(task="计算", draft="", tool_results=RECEIPTS, run_scope="child-turn", run_id="parent-child", call=call)
    leaf = {**result.verification, **persist_publication(tmp_path, result)}
    aggregate = {"run_id": "parent-child", "steps": {"analysis": leaf}}
    upstream = {"child": SimpleNamespace(data={"verification": aggregate})}
    assert upstream_evidence(tmp_path, upstream, "parent-child") == ([], [])
    assert upstream_evidence(tmp_path, upstream, "parent-child", trusted_tree=True) == (RECEIPTS, [])
    aggregate["steps"]["analysis"] = {**leaf, "run_id": "unrelated"}
    assert upstream_evidence(tmp_path, upstream, "parent-child", trusted_tree=True)[0] == []


def test_failed_structural_repair_keeps_previously_verified_items():
    calls = []
    def call(system, user):
        payload = json.loads(user)
        calls.append(payload)
        if "inferences" in payload:
            return '{"reviews":[],"coverage":{"complete":false,"missing":["尚有一项未完成"]}}'
        if len(calls) > 1:
            return "broken json"
        valid = compose(payload)
        valid["items"].append({"id": "bad", "section": "market", "type": "fact", "ref": "unknown"})
        return json.dumps(valid)
    result = publish_report(task="计算并核对另一项", draft="", tool_results=RECEIPTS, run_scope="x", call=call)
    assert "13.03%" in result.text and result.verification["status"] == "incomplete"
    assert len(calls) == 4


@pytest.mark.parametrize('wrap,complete', [
    (lambda s: '```json\n' + s + '\n```', True),
    (lambda s: s + '\n{}', False),
    (lambda s: '说明：' + s, False),
    (lambda s: s.replace('"schema_version": 1', '"schema_version": 1, "schema_version": 2'), False),
])
def test_only_json_transport_framing_is_accepted(wrap, complete):
    model, _ = call_model()
    def call(system, user):
        return wrap(model(system, user))
    result = publish_report(task='计算价格变化', draft='', tool_results=RECEIPTS, run_scope='frame', call=call)
    assert (result.verification['status'] == 'complete') is complete


def test_semantic_review_receives_complete_owned_receipt_not_only_selected_fields():
    raw = json.dumps({'symbol': '000001', 'macd': 0.5, 'signal': 0.3,
                      'signal_state': {'golden_cross': True, 'previous_macd': 0.1, 'previous_signal': 0.2}})
    def call(system, user):
        payload = json.loads(user)
        if 'schema' in payload:
            anchor = next(f for f in report_facts(payload) if f['metric'] == 'symbol')
            return json.dumps({'schema_version': 1, 'items': [{'id': 'judgment', 'type': 'inference',
                'section': 'assessment', 'text': 'MACD出现金叉，仍需结合其他条件评估。', 'refs': [anchor['id']]}]})
        source = payload['receipts'][0]
        assert 'MACD出现金叉' in payload['report']
        assert source['result'] == raw
        assert source['id'] == payload['inferences'][0]['evidence'][0]['receipt_id']
        assert json.loads(source['result'])['signal_state']['golden_cross'] is True
        return '{"reviews":[{"id":"judgment","supported":true,"reason":""}],"coverage":{"complete":true,"missing":[]}}'
    result = publish_report(task='核对MACD状态', draft='', tool_results=[('compute_macd',raw)], run_scope='full-source', call=call)
    assert result.verification['status'] == 'complete' and 'MACD出现金叉' in result.text


@pytest.mark.parametrize("inference_first", [False, True])
@pytest.mark.parametrize("supported", [False, True])
def test_judgment_can_use_verified_calculation_without_losing_original_evidence(inference_first, supported):
    receipts = [("get_bars", json.dumps({"symbol": "002415", "bars": [
        {"timestamp": "2026-11-10", "close": 34.83}, {"timestamp": "2026-11-11", "close": 34.25}]}))]
    calls = []

    def call(system, user):
        payload = json.loads(user)
        calls.append(payload)
        if "schema" in payload:
            document = compose(payload)
            item = {"id": "direction", "type": "inference", "section": "assessment",
                    "text": "收盘价较前一交易日下降。", "refs": ["change"]}
            document["items"].insert(0 if inference_first else 1, item)
            return json.dumps(document)
        judgment = payload["inferences"][0]
        calculation = judgment["calculations"][0]
        assert calculation["id"] == "change" and calculation["operation"] == "change_pct"
        assert calculation["result"].startswith("-1.665") and calculation["unit"] == "%"
        assert calculation["refs"] == [f["id"] for f in judgment["evidence"]]
        assert [f["value"] for f in judgment["evidence"]] == ["34.83", "34.25"]
        assert all(f["observed_at"] and f["pointer"] and f["receipt_id"] for f in judgment["evidence"])
        assert payload["receipts"][0]["result"] == receipts[0][1]
        return json.dumps({"reviews": [{"id": "direction", "supported": supported,
                                      "reason": "" if supported else "不能据此推断后续方向"}],
                           "coverage": {"complete": True, "missing": []}})

    result = publish_report(task="比较两日价格变化", draft="", tool_results=receipts, run_scope="derived", call=call)
    assert len(calls) == 2 and "-1.67%" in result.text
    assert (result.verification["status"] == "complete") is supported
    assert ("研判：收盘价" in result.text) is supported
    assert result.artifact["calculations"]["change"]["result"].startswith("-1.665")


@pytest.mark.parametrize("bad_reference", ["unknown", "self", "cycle", "other_symbol", "condition", "collision", "duplicate"])
def test_calculation_reference_never_launders_invalid_evidence(bad_reference):
    reviews = []

    def call(system, user):
        payload = json.loads(user)
        if "schema" in payload:
            document = compose(payload)
            calc = document["items"][0]
            calc["refs"] = calc["refs"][:2]
            target = calc["id"]
            if bad_reference == "unknown":
                target = "unowned-calculation"
            elif bad_reference == "self":
                calc["refs"][0] = target
            elif bad_reference == "cycle":
                document["items"].append({**calc, "id": "second", "refs": [target, calc["refs"][1]]})
                calc["refs"][0] = "second"
            elif bad_reference == "other_symbol":
                calc["refs"][1] = next(f["id"] for f in report_facts(payload) if f.get("subject") == "000001" and f["metric"] == "close")
            elif bad_reference == "condition":
                target = "planned"
                document["items"].append({"id": target, "section": "plan", "type": "condition", "ref": calc["refs"][0],
                                          "comparison": "lt", "threshold": "6", "unit": "元/股", "action": "review"})
            elif bad_reference == "collision":
                calc["id"] = calc["refs"][0]
                target = calc["id"]
            else:
                document["items"].append(dict(calc))
            document["items"].append({"id": "judgment", "type": "inference", "section": "assessment",
                                      "text": "未经支持的结论", "refs": [target]})
            return json.dumps(document)
        reviews.extend(payload["inferences"])
        return '{"reviews":[],"coverage":{"complete":true,"missing":[]}}'

    other = ("get_bars", json.dumps({"symbol": "000001", "bars": [{"timestamp": "2026-11-11", "close": 8}]}))
    result = publish_report(task="比较", draft="", tool_results=[*RECEIPTS, other], run_scope="invalid-derived", call=call)
    assert result.verification["status"] == "incomplete"
    assert not reviews and "研判：未经支持的结论" not in result.text


@pytest.mark.parametrize("first", [True, False])
@pytest.mark.parametrize("supported", [True, False])
def test_observation_reference_retains_source_and_independent_semantic_review(first, supported):
    def call(system, user):
        payload = json.loads(user)
        if "schema" in payload:
            fact = next(f for f in report_facts(payload) if f['metric'] == 'close')
            items = [
                {'id': 'observation', 'section': 'market', 'type': 'fact', 'ref': fact['id']},
                {'id': 'judgment', 'section': 'assessment', 'type': 'inference',
                 'text': '该价位仍需结合其他时点评估。', 'refs': ['observation']},
            ]
            return json.dumps({'schema_version': 1, 'items': items if first else items[::-1]})
        evidence = payload['inferences'][0]['evidence']
        assert len(evidence) == 1 and evidence[0]['value'] == '6.6'
        assert evidence[0]['pointer'] == '/bars/0/close'
        assert payload['receipts'][0]['result'] == RECEIPTS[0][1]
        return json.dumps({'reviews': [{'id': 'judgment', 'supported': supported,
                         'reason': '' if supported else '依据不足'}],
                         'coverage': {'complete': True, 'missing': []}})
    result = publish_report(task='列出价格及限制', draft='', tool_results=RECEIPTS, run_scope='observation', call=call)
    assert (result.verification['status'] == 'complete') is supported
    assert ('研判：该价位' in result.text) is supported


@pytest.mark.parametrize('target', ['unknown', 'observation', 'other'])
def test_observation_alias_does_not_accept_unowned_or_recursive_evidence(target):
    reviewed = []
    def call(system, user):
        payload = json.loads(user)
        if 'schema' in payload:
            return json.dumps({'schema_version': 1, 'items': [
                {'id': 'observation', 'section': 'market', 'type': 'fact', 'ref': target},
                {'id': 'other', 'section': 'market', 'type': 'fact', 'ref': 'observation'},
                {'id': 'judgment', 'section': 'assessment', 'type': 'inference',
                 'text': '不应发布', 'refs': ['observation']},
            ]})
        reviewed.extend(payload['inferences'])
        return '{"reviews":[],"coverage":{"complete":false,"missing":["事实未核实"]}}'
    result = publish_report(task='列出价格', draft='', tool_results=RECEIPTS, run_scope='invalid-observation', call=call)
    assert not reviewed and result.verification['status'] == 'incomplete'
    assert '研判：不应发布' not in result.text
