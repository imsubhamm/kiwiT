from copy import deepcopy
from datetime import timedelta

from test_playbooks import NOW, fixtures

from kiwit.options_evaluation import compare, counterfactual, exit_matrix, rule_matrix, session_acceptance


def evidence():
    snapshot, _, _ = fixtures()
    plan = snapshot['strategy_selection']['plans'][0]
    tape = []
    for minutes in (0, 15):
        contract = deepcopy(snapshot['candidates'][0])
        at = NOW + timedelta(minutes=minutes)
        contract['quote']['stamp'] = at.isoformat()
        tape.append({'recorded_at': at.isoformat(), 'market_snapshot': {'candidates': [contract],
                     'spot': snapshot['spot'], 'spot_at': at.isoformat()}})
    return snapshot, plan, tape


def test_forward_quotes_and_cost_stress():
    _, plan, tape = evidence()
    result = counterfactual(plan, tape, NOW, 15)
    assert result['status'] == 'observed'
    stressed = counterfactual(plan, tape, NOW, 15, 10)
    from decimal import Decimal
    assert Decimal(stressed['net_pnl']) < Decimal(result['net_pnl']) < 0


def test_missing_future_quotes_and_tampered_quantity_excluded():
    _, plan, tape = evidence()
    assert counterfactual(plan, tape[:1], NOW, 15)['status'] == 'excluded'
    changed = dict(plan, quantity=plan['quantity'] + 30)
    assert counterfactual(changed, tape, NOW, 15)['reason'] == 'plan_integrity'
    tape[0]['recorded_at'] = (NOW - timedelta(seconds=1)).isoformat()
    assert counterfactual(plan, tape, NOW, 15)['status'] == 'excluded'


def test_underlying_invalidated_after_decision_is_not_a_counterfactual_fill():
    _, plan, tape = evidence()
    tape[0]['market_snapshot']['spot'] = '1'
    assert counterfactual(plan, tape, NOW, 15)['status'] == 'excluded'


def test_pairing_requires_response_time_and_complete_baseline():
    snapshot, _, tape = evidence()
    call = {'call_id': 'fixture', 'state': 'applied', 'snapshot': snapshot,
            'result': {'decision': {'action': 'HOLD'}}}
    bundle = {'calls': [call], 'market_tape': tape}
    assert compare(bundle)['comparisons'][0]['reason'] == 'response_time_missing'
    call['result']['settled_at'] = NOW.isoformat()
    assert compare(bundle)['experiments'][0]['pairs'] == 1
    bundle['market_tape'] = tape[:1]
    assert compare(bundle)['experiments'] == []


def test_calendar_shadow_plan_is_measured_without_an_ai_call():
    snapshot, plan, tape = evidence()
    for minutes in (30, 45):
        contract = deepcopy(snapshot['candidates'][0])
        at = NOW + timedelta(minutes=minutes)
        contract['quote']['stamp'] = at.isoformat()
        tape.append({'recorded_at': at.isoformat(), 'market_snapshot': {'candidates': [contract],
                     'spot': snapshot['spot'], 'spot_at': at.isoformat()}})
    shadow = deepcopy(plan)
    shadow.update(evidence_only=True, execution_eligible=False,
                  block_reason_codes=['HIGH_IMPACT_EVENT_WINDOW'],
                  measurement_context={'capital': '100000', 'retention_minutes': 60})
    from kiwit.playbooks import fingerprint
    shadow['id'] = fingerprint({key: value for key, value in shadow.items() if key != 'id'})
    event = {'trading_date': '2026-08-26', 'event_at': NOW.isoformat(), 'kind': 'strategy_scan',
             'detail': {'plans': [], 'shadow_plans': [shadow]}}
    bundle = {'calls': [], 'events': [event], 'market_tape': tape}
    rules = rule_matrix(bundle)
    exits = exit_matrix(bundle)
    assert rules['observed_opportunities'] == 1
    assert exits['attempted'] == 3
    assert exits['excluded'] == 0


def test_acceptance_does_not_accept_reserved_call_or_partial_exit():
    snapshot, _, _ = evidence()
    snapshot['provenance'] = {'release': 'abc'}
    bundle = {'calls': [{'trading_date': '2026-08-26', 'call_id': 'a', 'state': 'reserved',
                         'snapshot': snapshot}], 'events': [], 'reports': []}
    result = session_acceptance(bundle, '2026-08-26', 'abc')
    assert result['status'] == 'incomplete' and not result['checks']['decision']


def scan_evidence(reason=None):
    snapshot, plan, tape = evidence()
    event = {'event_id': 1, 'trading_date': '2026-08-26', 'event_at': NOW.isoformat(),
             'kind': 'strategy_scan', 'detail': {
                 **deepcopy(snapshot['strategy_selection']),
                 'measurement_context': {'capital': '100000'},
                 'entry_gate': {'allowed': reason is None, 'reason_codes': [reason] if reason else []},
                 'decision_event': {'new': False},
                 'provenance': {'release': 'fixture'}}}
    return snapshot, plan, {'calls': [], 'events': [event], 'market_tape': tape}


def test_cap_cooldown_and_no_call_scans_are_included_without_execution_authority():
    for reason in ('DAILY_ENTRY_CAP', 'POST_EXIT_COOLDOWN', None):
        _, _, bundle = scan_evidence(reason)
        original = deepcopy(bundle)
        result = rule_matrix(bundle)
        assert result['attempted'] == result['included'] == 1
        assert result['excluded'] == 0
        row = result['opportunities'][0]
        assert row['source'] == 'strategy_scan'
        assert row['block_reason_codes'] == ([reason] if reason else [])
        assert row['provenance'] == {'release': 'fixture'}
        assert row['available_at'] == NOW.isoformat()
        assert row['execution_eligible'] is False and row['evidence_only'] is True
        assert bundle == original
        assert exit_matrix(bundle)['attempted'] == 3


def test_scan_call_duplicates_use_scan_time_but_later_scans_are_distinct():
    snapshot, _, bundle = scan_evidence()
    snapshot['capital'] = '100000'
    bundle['calls'] = [{'call_id': 'a', 'trading_date': '2026-08-26', 'snapshot': snapshot,
                        'result': {'settled_at': (NOW + timedelta(minutes=1)).isoformat()}}]
    bundle['events'].append(deepcopy(bundle['events'][0]))
    result = rule_matrix(bundle)
    assert result['attempted'] == result['included'] == 1
    identity = result['opportunities'][0]['occurrence_id']
    later = deepcopy(bundle['events'][0])
    later['event_id'] = 2
    later['event_at'] = later['detail']['at'] = (NOW + timedelta(seconds=30)).isoformat()
    bundle['events'].append(later)
    result = rule_matrix(bundle)
    assert result['attempted'] == 2
    assert len({r['occurrence_id'] for r in result['opportunities']}) == 2
    assert result['opportunities'][0]['occurrence_id'] == identity
    assert result['excluded'] == 1  # No eligible forward entry quote for the later scan.


def test_missing_context_and_quotes_remain_visible_in_each_rule():
    _, _, bundle = scan_evidence('DAILY_ENTRY_CAP')
    for capital in (None, 'NaN', '0', '-1'):
        bundle['events'][0]['detail']['measurement_context']['capital'] = capital
        result = rule_matrix(bundle)
        assert result['attempted'] == result['excluded'] == 1
        assert result['opportunities'][0]['outcome']['reason'] == 'capital_missing_or_invalid'
        for scenario in result['scenarios']:
            assert scenario['attempted'] == scenario['included'] + scenario['excluded'] == 1
    bundle['events'][0]['detail']['measurement_context']['capital'] = '100000'
    bundle['market_tape'] = []
    result = rule_matrix(bundle)
    assert result['opportunities'][0]['outcome']['reason'] == 'future_entry_quote_or_depth_missing'
    exits = exit_matrix(bundle)
    assert exits['attempted'] == exits['excluded'] == 3
    assert all(row['excluded'] == 1 and row['included'] == 0 for row in exits['scenarios'])


def test_rule_cap_exclusions_are_counted_separately_from_quote_exclusions():
    _, _, bundle = scan_evidence()
    first = deepcopy(bundle['events'][0])
    # Separate playbook opportunities available at the same instant.
    from kiwit.playbooks import fingerprint
    for n in range(1, 6):
        event = deepcopy(first)
        plan = event['detail']['plans'][0]
        plan['playbook_id'] = f'fixture-{n}'
        plan['id'] = fingerprint({k: v for k, v in plan.items() if k != 'id'})
        bundle['events'].append(event)
    result = rule_matrix(bundle)
    assert result['included'] == 6
    scenario = result['scenarios'][0]
    assert scenario['attempted'] == 6 and scenario['included'] == 4 and scenario['excluded'] == 2
    assert {row['reason'] for row in scenario['exclusions']} == {'scenario_entry_cap'}


def test_call_only_evidence_uses_response_availability_and_missing_time_is_reported():
    snapshot, _, bundle = scan_evidence()
    snapshot['capital'] = '100000'
    bundle['events'] = []
    bundle['calls'] = [{'call_id': 'a', 'trading_date': '2026-08-26', 'snapshot': snapshot,
                        'result': {'settled_at': NOW.isoformat()}}]
    result = rule_matrix(bundle)
    assert result['included'] == 1
    assert result['opportunities'][0]['source'] == 'ai_call'
    bundle['calls'][0]['result'] = {}
    result = rule_matrix(bundle)
    assert result['excluded'] == 1
    assert result['opportunities'][0]['outcome']['reason'] == 'availability_time_missing_or_invalid'


def test_legacy_scan_capital_recovery_requires_the_exact_selection():
    snapshot, _, bundle = scan_evidence('POST_EXIT_COOLDOWN')
    del bundle['events'][0]['detail']['measurement_context']
    snapshot['capital'] = '100000'
    bundle['market_tape'][0]['trading_date'] = '2026-08-26'
    bundle['market_tape'][0]['market_snapshot'].update(snapshot)
    result = rule_matrix(bundle)
    assert result['included'] == 1
    assert result['opportunities'][0]['capital_source'] == 'market_snapshot'
    bundle['market_tape'][0]['market_snapshot']['strategy_selection']['at'] = (
        NOW + timedelta(minutes=1)).isoformat()
    result = rule_matrix(bundle)
    assert result['excluded'] == 1
    assert result['opportunities'][0]['outcome']['reason'] == 'capital_missing_or_invalid'
