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


def is_current_observation(statement, report_context):
    """Resolve positive typed anchors without changing the saved interpretation."""
    expected = report_context['reference_date']
    dates = statement.get('observation_dates', [])
    # The blind reader may resolve "today" to the actual calendar date.
    # It retains the same obligation as task_now. Do not collapse historical
    # or multi-date comparisons into the task's present.
    if statement['time_scope'] != 'reference_date' and _record_observation(statement):
        return False
    return statement['time_scope'] == 'reference_date' or bool(expected) and dates == [expected]


def _record_observation(statement):
    return (len(statement.get('observation_dates', [])) == 1
            and 'task_now' not in statement['observation_dates']
            and any(c['property'] == 'observation_scope' and c['value'] == 'record_date'
                    for c in statement.get('classifications', [])))


def check_statement_time(statement, assessment, context, report_context):
    expected = report_context['reference_date']
    current = is_current_observation(statement, report_context)
    # Only runtime-rendered date bindings have a mechanically known display
    # calendar. Author-specified calendars remain with factual/source review.
    dated_record = (not current and _record_observation(statement)
                    and bool(statement.get('bound_date_refs')))
    timezone = report_context['reference_timezone']
    if dated_record:
        expected = statement['observation_dates'][0]
        timezone = report_context.get('display_timezone', 'Asia/Shanghai')
    result = {'scope': statement['time_scope'], 'expected_date': expected,
              'comparison_timezone': timezone,
              'evidence': [], 'status': 'not_applicable', 'reason': 'not_current_observation'}
    if 'unresolved' in statement.get('observation_dates', []):
        result.update(status='unknown', reason='asserted_date_unresolved')
        return result
    if not current and not dated_record:
        return result
    if (statement['assertion_kind'] in {'judgment', 'discourse'}
            and 'task_now' not in statement.get('observation_dates', [])
            and statement.get('observation_dates', []) != [expected]
            and assessment['checks']['quantity'] == 'not_applicable'
            and not statement['bound_fact_refs'] and not assessment['time_evidence']):
        # A present opinion is not a new dated measurement. Bound or asserted
        # quantities and explicit empirical calendar anchors still require their
        # own observation-time proof, including qualitative measurements.
        result['reason'] = 'non_observational_judgment'
        return result
    facts = {fact['id']: fact for fact in context['evidence']}
    # A derived price change describes the interval ending at its later operand.
    bound = set(statement['bound_fact_refs'])
    refs = list(dict.fromkeys([*statement['time_fact_refs'],
        *(statement.get('bound_date_refs', []) if dated_record else []),
        *(ref for ref in assessment['time_evidence'] if ref not in bound)]))
    if dated_record:
        date_refs = statement['bound_date_refs']
        values = [facts[ref] for ref in statement['time_fact_refs']]
        def record_key(fact):
            return (fact.get('entity'), fact.get('account'),
                    observation_date(fact['observed_at'], timezone))
        # A displayed record date belongs to its source record. It must not be
        # borrowed for a different sole value, or spread across other dated
        # measurements in an analytical comparison. Their semantic date/scope
        # checks remain independent; task-present obligations above stay strict.
        if values and any(not any(record_key(facts[ref]) == record_key(value)
                                  for value in values) for ref in date_refs):
            result.update(status='unknown', reason='record_date_value_reference_mismatch')
            return result
        refs = date_refs
    observations = [facts[ref] for ref in refs if facts[ref]['kind'] != 'identity']
    snapshots = PORTFOLIO_OPERATIONS | {'analyze_portfolio'} | READ_RESULT_OPERATIONS
    if observations and all(fact['source'] in snapshots for fact in observations):
        # Account snapshots retain their existing run-ownership contract.
        result['reason'] = ('read_result_snapshot' if any(fact['source'] in READ_RESULT_OPERATIONS
                                                        for fact in observations) else 'account_snapshot')
        return result
    observations = [fact for fact in observations if fact['source'] not in snapshots]
    result['evidence'] = [{'ref': fact['id'], 'observed_date': fact['observed_date'],
                          'comparison_date': observation_date(fact['observed_at'], timezone)}
                          for fact in observations]
    if not expected:
        result.update(status='unknown', reason='reference_date_unavailable')
    elif not observations:
        result.update(status='unknown', reason='observation_unavailable')
    elif (statement['assertion_kind'] == 'comparison'
            and set(statement.get('observation_dates', [])) - {'task_now', 'unresolved'}):
        declared = set(statement['observation_dates']) - {'task_now', 'unresolved'} | {expected}
        result['declared_dates'] = sorted(declared)
        matched = (any(row['comparison_date'] == expected for row in result['evidence'])
                   and all(row['comparison_date'] in declared for row in result['evidence']))
        result.update(status='entailed' if matched else 'unknown',
                      reason='matched_comparison_dates' if matched else 'observation_not_at_asserted_date')
    elif any(row['comparison_date'] != expected for row in result['evidence']):
        result.update(status='unknown', reason='observation_not_at_asserted_date')
    else:
        result.update(status='entailed', reason='matched_observation_date')
    return result


def unavailable_bound_observation_dates(statement, assessment, timing):
    """Identify a sole date-availability failure; never approve its claim.

    Unknown reviewer references or metadata are handled by the caller first.
    A known stale observation or another unsupported dimension must retain its
    factual failure rather than becoming a missing-data notice.
    """
    if (timing['status'] != 'unknown' or timing['reason'] != 'observation_not_at_asserted_date'
            or assessment['verdict'] == 'contradicted'
            or assessment['checks']['time'] != 'unknown'
            or any(value not in {'entailed', 'not_applicable'}
                   for key, value in assessment['checks'].items() if key != 'time')):
        return []
    conflicts = [row for row in timing['evidence'] if row['comparison_date'] != timing['expected_date']]
    if any(row['comparison_date'] is not None for row in conflicts):
        return []
    # A verified value can be written literally as well as through a binding.
    # Its completed assessment may nominate the same owned observation. Caller
    # validation rejects foreign/unavailable witnesses; the guards above retain
    # stale dates and every substantive non-time failure as factual failures.
    mandatory = set(statement['time_fact_refs']) | set(assessment['time_evidence'])
    return [row['ref'] for row in conflicts if row['ref'] in mandatory]
