"""Bounded reconciliation of inconsistent semantic annotations, never prose."""
from __future__ import annotations

from trade_compass_agent.runtime.report_assessments import assessments_with_availability


ANNOTATION_REPAIR = """The annotation_conflicts are unresolved disagreements about the exact reader text, not proof that its claims are true. Resolve them independently using the original paragraphs, source contexts and receipts. Do not rewrite prose, change observations or satisfy a task by changing its meaning.
For each listed conflict return annotation_resolutions entries with id, empirical_present (boolean), and assessment. An assessment contains id, comparison (source comparison), verdict (entailed|contradicted|unknown), support_basis (direct|calculated|reasoned|unknown|not_applicable), commitments ({participant_activity:boolean,causal_relation:boolean}), time_evidence (owned fact IDs), and checks ({identity_scope,time,quantity,support}, each entailed|contradicted|unknown|not_applicable). Return [] for conflicts you cannot resolve. Use only supplied statement IDs and owned fact references. Every assessment is a fresh source review; no prior interpretation or verdict is supplied as authority. Do not assume that a selected statement is an opinion or that it is supported.
A current research opinion is not automatically a new current market measurement. An opinion's actual empirical premises, even qualitative or embedded ones, still require their real dates. empirical_present=false only if the exact statement adds no present empirical observation; an assessment of holding rationale is distinct from an investor's actual beliefs/actions. Do not erase explicit current values or qualitative observations by calling them opinions.
Distinguish analytical use of an evidenced event from a claim that it actually caused price action. A reasoned interpretation may be supported without asserting causation or observed participant activity. Preserve all actual actor/causal predicates, including negation; aggregate volume cannot prove absence of an actor. Explain the reader's actual commitment and its source support in comparison. A previous positive verdict or a named source never establishes truth.
Coverage still independently checks the complete reader report. annotation_resolutions may repair only the listed annotations; they cannot clear a bound value/date conflict, invent a source or rewrite any reader text. The runtime applies its normal source-ownership and factual checks again.
For a time_evidence conflict, preserve the original empirical time commitment: empirical_present must be true and supply owned time_evidence for the SAME subject, metric/property and observation scope. A fresh unrelated fact, retrieval timestamp or account snapshot cannot refresh a different stale market observation. Do not omit witnesses merely because the source supports the value or the interpretation is qualitative. An unavailable or stale observation remains unknown; never invent its date or erase the current claim by calling it an opinion.
本次还必须完成第四项独立核验：annotation_review 中包含待核对的原文、原始依据和 annotation_conflicts。不要只返回任务覆盖结果。最终顶层 JSON 必须同时包含 reviews、coverage、annotation_resolutions；有 gaps 时仍保留前述 gap_reviews 和 coverage 的缺口依赖字段。annotation_resolutions 与 coverage 并列，不放进 coverage 或 annotation_review。
结构示意：{"reviews":[],"coverage":{"complete":true,"missing":[],"repairable":false},"annotation_resolutions":[{"id":"实际待核对id","empirical_present":false,"assessment":{"id":"同一id","comparison":"原文承诺与原始依据的对照","verdict":"entailed","support_basis":"reasoned","commitments":{"participant_activity":false,"causal_relation":false},"time_evidence":[],"checks":{"identity_scope":"not_applicable","time":"not_applicable","quantity":"not_applicable","support":"entailed"}}}]}。
示意中的值不是答案。逐项独立核对，保留真实的观察、参与者行为和因果承诺；资料或含义不足时保留 unknown，无法完成的条目可省略，annotation_resolutions 仍须存在。不得因 coverage 不完整而跳过这些原文核验。
"""


def annotation_conflicts(statements, assessments, errors):
    """Select narrowly attributable annotation faults, not arbitrary failures."""
    by_id = {row['id']: row for row in statements}
    result = {}
    for row in assessments:
        statement = by_id[row['id']]
        reported = row.get('reported_checks', row['checks'])
        error = errors.get(row['id'])
        if error and (error['code'] != 'temporal_witness_missing' or error.get('additional_codes')):
            continue
        commitments = row.get('commitments')
        if (not isinstance(commitments, dict) or set(commitments) != {'participant_activity', 'causal_relation'}
                or any(type(value) is not bool for value in commitments.values())):
            continue
        valid_report = (isinstance(reported, dict)
            and all(value in {'entailed', 'not_applicable'} for value in reported.values())
            and row.get('reported_verdict', row['verdict']) == 'entailed')
        pure_opinion = (statement['assertion_kind'] == 'judgment' and statement['meaning_kind'] == 'other'
            and not statement['bound_fact_refs'] and not statement['time_fact_refs']
            and not statement.get('classifications') and row.get('support_basis') == 'reasoned'
            and not any(commitments.values()) and valid_report
            and all(reported[key] == 'not_applicable' for key in ('identity_scope', 'time', 'quantity')))
        if (error and valid_report and not pure_opinion and statement['time_scope'] == 'reference_date'
                and not statement['time_fact_refs'] and row.get('support_basis') in {'direct', 'calculated', 'reasoned'}):
            result[row['id']] = 'temporal_witness'
            continue
        # An owned, already linked old/unknown source date is a real conflict,
        # not an annotation fault that can be erased by another interpretation.
        if not error and row['checks']['time'] == 'unknown':
            continue
        if (statement['assertion_kind'] != 'judgment' or statement['meaning_kind'] != 'other'
                or statement['bound_fact_refs'] or statement['time_fact_refs']
                or statement.get('classifications') or row.get('support_basis') != 'reasoned'
                or row.get('reported_verdict', row['verdict']) != 'entailed'
                or not isinstance(reported, dict)
                or any(value not in {'entailed', 'not_applicable'} for value in reported.values())):
            continue
        commitments = row.get('commitments')
        if (not isinstance(commitments, dict) or set(commitments) != {'participant_activity', 'causal_relation'}
                or any(type(value) is not bool for value in commitments.values())):
            continue
        if any(commitments.values()):
            result[row['id']] = 'empirical_scope'
        elif (statement['time_scope'] == 'reference_date' and not row['time_evidence']
                and all(reported[key] == 'not_applicable' for key in ('identity_scope', 'time', 'quantity'))):
            result[row['id']] = 'opinion_time'
    return result


def resolved_annotations(value, candidates, statements, decode):
    """Invalid optional repairs leave original failures and usable siblings intact."""
    if not isinstance(value, list) or len(value) > len(candidates):
        raise ValueError('invalid annotation resolutions')
    seen, result = set(), {}
    by_id = {row['id']: row for row in statements}
    for entry in value:
        if (not isinstance(entry, dict) or set(entry) != {'id', 'empirical_present', 'assessment'}
                or not isinstance(entry['id'], str) or entry['id'] not in candidates or entry['id'] in seen
                or type(entry['empirical_present']) is not bool):
            raise ValueError('invalid annotation resolution identity')
        identifier = entry['id']
        seen.add(identifier)
        rows, errors = assessments_with_availability(decode({'assessments': [entry['assessment']]}), [identifier])
        if errors:
            continue
        assessment = rows[0]
        statement = dict(by_id[identifier])
        if candidates[identifier] == 'temporal_witness':
            if not entry['empirical_present'] or not assessment['time_evidence']:
                continue
            result[identifier] = (statement, assessment)
            continue
        # An optional metadata repair cannot remove a positive body requirement
        # about an actor, cause, owned value or indicator classification. Only a
        # mutually identified opinion with no new identity/quantity/time claim
        # can correct its inconsistent present-observation annotation.
        if not entry['empirical_present'] and statement['time_scope'] == 'reference_date':
            if (assessment.get('support_basis') != 'reasoned' or assessment['time_evidence']
                    or assessment.get('commitments') != {'participant_activity': False, 'causal_relation': False}
                    or assessment['checks']['quantity'] != 'not_applicable'
                    or any(assessment['checks'][key] not in {'entailed', 'not_applicable'}
                           for key in ('identity_scope', 'time'))):
                continue
            assessment['reported_checks'] = dict(assessment['checks'])
            assessment['checks']['time'] = 'not_applicable'
            statement.update(time_scope='other', observation_dates=[])
        result[identifier] = (statement, assessment)
    return result
