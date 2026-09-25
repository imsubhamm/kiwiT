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
