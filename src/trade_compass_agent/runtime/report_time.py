"""Compare interpreted observation scope with owned dates; never parse prose."""
from __future__ import annotations

from trade_compass_agent.runtime.report_contract import PORTFOLIO_OPERATIONS


def check_statement_time(statement, assessment, context, report_context):
    expected = report_context['reference_date']
    result = {'scope': statement['time_scope'], 'expected_date': expected,
              'evidence': [], 'status': 'not_applicable', 'reason': 'not_current_observation'}
    if statement['time_scope'] != 'reference_date':
        return result
    facts = {fact['id']: fact for fact in context['evidence']}
    # A derived price change describes the interval ending at its later operand.
    bound = set(statement['bound_fact_refs'])
    refs = list(dict.fromkeys([*statement['time_fact_refs'],
        *(ref for ref in assessment['time_evidence'] if ref not in bound)]))
    observations = [facts[ref] for ref in refs if facts[ref]['kind'] != 'identity']
    snapshots = PORTFOLIO_OPERATIONS | {'analyze_portfolio'}
    if observations and all(fact['source'] in snapshots for fact in observations):
        # Account snapshots retain their existing run-ownership contract.
        result['reason'] = 'account_snapshot'
        return result
    observations = [fact for fact in observations if fact['source'] not in snapshots]
    result['evidence'] = [{'ref': fact['id'], 'observed_date': fact['observed_date']}
                          for fact in observations]
    if not expected:
        result.update(status='unknown', reason='reference_date_unavailable')
    elif not observations:
        result.update(status='unknown', reason='observation_unavailable')
    elif any(fact['observed_date'] != expected for fact in observations):
        result.update(status='unknown', reason='observation_not_at_asserted_date')
    else:
        result.update(status='entailed', reason='matched_observation_date')
    return result
