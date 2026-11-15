"""Compare interpreted observation scope with owned dates; never parse prose."""
from __future__ import annotations

from trade_compass_agent.runtime.report_contract import PORTFOLIO_OPERATIONS, READ_RESULT_OPERATIONS, observation_date


def temporal_evidence_context(context, contexts):
    """Source ownership belongs to the run, not the writer's paragraph selection.

    Comparisons may legitimately cross securities, accounts and periods.
    Membership establishes provenance only; semantic scope and all original
    bound observations still require their independent checks.
    """
    facts = {fact['id']: fact for fact in context['evidence']}
    for other in contexts.values():
        for fact in other['evidence']:
            facts.setdefault(fact['id'], fact)
    return {**context, 'evidence': list(facts.values())}


def check_statement_time(statement, assessment, context, report_context):
    expected = report_context['reference_date']
    result = {'scope': statement['time_scope'], 'expected_date': expected,
              'comparison_timezone': report_context['reference_timezone'],
              'evidence': [], 'status': 'not_applicable', 'reason': 'not_current_observation'}
    if 'unresolved' in statement.get('observation_dates', []):
        result.update(status='unknown', reason='asserted_date_unresolved')
        return result
    if statement['time_scope'] != 'reference_date':
        return result
    if (statement['assertion_kind'] in {'judgment', 'discourse'}
            and assessment['checks']['quantity'] == 'not_applicable'
            and not statement['bound_fact_refs'] and not assessment['time_evidence']):
        # A present opinion is not a new dated measurement. Bound or asserted
        # quantities still require their own observation-time proof.
        result['reason'] = 'non_observational_judgment'
        return result
    facts = {fact['id']: fact for fact in context['evidence']}
    # A derived price change describes the interval ending at its later operand.
    bound = set(statement['bound_fact_refs'])
    refs = list(dict.fromkeys([*statement['time_fact_refs'],
        *(ref for ref in assessment['time_evidence'] if ref not in bound)]))
    observations = [facts[ref] for ref in refs if facts[ref]['kind'] != 'identity']
    snapshots = PORTFOLIO_OPERATIONS | {'analyze_portfolio'} | READ_RESULT_OPERATIONS
    if observations and all(fact['source'] in snapshots for fact in observations):
        # Account snapshots retain their existing run-ownership contract.
        result['reason'] = ('read_result_snapshot' if any(fact['source'] in READ_RESULT_OPERATIONS
                                                        for fact in observations) else 'account_snapshot')
        return result
    observations = [fact for fact in observations if fact['source'] not in snapshots]
    result['evidence'] = [{'ref': fact['id'], 'observed_date': fact['observed_date'],
                          'comparison_date': observation_date(fact['observed_at'], report_context['reference_timezone'])}
                          for fact in observations]
    if not expected:
        result.update(status='unknown', reason='reference_date_unavailable')
    elif not observations:
        result.update(status='unknown', reason='observation_unavailable')
    elif any(row['comparison_date'] != expected for row in result['evidence']):
        result.update(status='unknown', reason='observation_not_at_asserted_date')
    else:
        result.update(status='entailed', reason='matched_observation_date')
    return result
