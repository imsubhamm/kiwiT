from copy import deepcopy
from datetime import datetime, timedelta

import pytest
from test_playbooks import NOW, fixtures

from kiwit.intraday import IST
from kiwit.options_evidence_gates import (
    fee_reconciliation,
    parse_contract_notes,
    release_acceptance,
    replay_exit_paths,
    unbiased_baselines,
)
from kiwit.options_risk import fill_price

COMPONENTS = ('brokerage', 'exchange', 'ipft', 'sebi', 'gst', 'stamp', 'stt', 'total')


def sample_contract(at, bid='80', ask='81'):
    return {
        'symbol': 'BANKNIFTY26SEP55000CE', 'kind': 'CE', 'expiry': '2026-09-29', 'strike': '55000',
        'lot': 30, 'freeze': 601, 'tick': '.05',
        'quote': {'stamp': at.isoformat(), 'bid': bid, 'ask': ask, 'bid_size': 60, 'ask_size': 60},
    }


def positioned_exit(at, bid='80'):
    item = sample_contract(at, bid=bid)
    price = fill_price(item['quote'], item, False)
    position = {
        'id': 'p1', 'quantity': 30, 'stop': str(price), 'target': '1000',
        'entered_at': NOW.isoformat(), 'exit_deadline': (NOW + timedelta(hours=2)).isoformat(),
        'exit_policy': 'playbook_cost_aware_v5', 'day': '2026-08-26',
        'contract': {key: value for key, value in item.items() if key != 'quote'},
        'entry_plan': {'kind': 'CE', 'underlying_invalidation': '1'},
    }
    tape = [{'recorded_at': at.isoformat(), 'market_snapshot': {
        'candidates': [item], 'spot': '55000', 'spot_at': at.isoformat()}}]
    recorded = {'kind': 'paper_exit', 'detail': {
        'position_id': 'p1', 'reason': 'stop_loss', 'price': str(price), 'quantity': 30,
        'quote': item['quote'], 'closed': True}}
    return {'events': [{'kind': 'paper_entry', 'detail': {'position': position}}, recorded],
            'market_tape': tape, 'calls': []}, price


def test_v5_exit_replay_matches_stop_and_lets_session_stop_win():
    bundle, _price = positioned_exit(NOW + timedelta(minutes=1))
    result = replay_exit_paths(bundle)
    assert result['status'] == 'pass'
    assert result['fixed_horizon_used'] is False
    assert result['live_orders'] == 'disabled'
    assert result['positions'][0]['replay']['fills'][0]['reason'] == 'stop_loss'

    late = datetime(2026, 8, 26, 15, 16, tzinfo=IST)
    session_bundle, price = positioned_exit(late)
    session_bundle['events'][1]['detail']['reason'] = 'session_stop'
    session_bundle['events'][1]['detail']['price'] = str(price)
    session_bundle['events'][1]['detail']['quote'] = session_bundle['market_tape'][0]['market_snapshot']['candidates'][0]['quote']
    replayed = replay_exit_paths(session_bundle)
    assert replayed['positions'][0]['replay']['fills'][0]['reason'] == 'session_stop'
    assert replayed['status'] == 'pass'

    missing = deepcopy(bundle)
    missing['market_tape'] = []
    assert replay_exit_paths(missing)['status'] == 'incomplete'


def test_baselines_share_one_stream_and_keep_rules_unchanged():
    snapshot, _state, _quote = fixtures()
    plan = deepcopy(snapshot['strategy_selection']['plans'][0])
    plan['block_reason_codes'] = ['HIGH_IMPACT_EVENT_WINDOW']
    event = {'event_id': '1', 'trading_date': '2026-08-26', 'event_at': NOW.isoformat(), 'kind': 'strategy_scan',
             'detail': {'plans': [plan], 'at': NOW.isoformat(), 'measurement_context': {'capital': '100000'},
                        'entry_gate': {'reason_codes': ['HIGH_IMPACT_EVENT_WINDOW']}}}
    blocked = unbiased_baselines({'events': [event], 'calls': [], 'market_tape': []})
    production = blocked['scenarios'][0]
    ignored = next(row for row in blocked['scenarios'] if row['name'] == 'event_window_ignored')
    assert production['name'] == 'production'
    assert blocked['rules_changed'] is False
    assert blocked['promotion_eligible'] is False
    assert next(row for row in production['policies'] if row['policy'] == 'deterministic')['vetoes']['HIGH_IMPACT_EVENT_WINDOW'] == 1
    assert next(row for row in production['policies'] if row['policy'] == 'hold')['trades'] == 0
    assert 'HIGH_IMPACT_EVENT_WINDOW' not in next(row for row in ignored['policies'] if row['policy'] == 'deterministic')['vetoes']

    entry_at = NOW + timedelta(seconds=30)
    exit_at = entry_at + timedelta(minutes=5)
    entry_contract = deepcopy(snapshot['candidates'][0])
    entry_contract['quote']['stamp'] = entry_at.isoformat()
    exit_contract = deepcopy(entry_contract)
    exit_contract['quote'] = {'stamp': exit_at.isoformat(), 'bid': '1', 'ask': '1.05', 'bid_size': 600, 'ask_size': 600}
    clean_plan = snapshot['strategy_selection']['plans'][0]
    scan = {'event_id': '2', 'trading_date': '2026-08-26', 'event_at': NOW.isoformat(), 'kind': 'strategy_scan',
            'detail': {'plans': [clean_plan], 'at': NOW.isoformat(), 'measurement_context': {'capital': '100000'},
                       'entry_gate': {'reason_codes': []}}}
    call = {'call_id': 'c', 'trading_date': '2026-08-26', 'state': 'applied', 'snapshot': snapshot,
            'result': {'settled_at': NOW.isoformat(), 'decision': {'action': 'BUY', 'plan_id': clean_plan['id']}}}
    tape = [
        {'recorded_at': entry_at.isoformat(), 'market_snapshot': {'candidates': [entry_contract], 'spot': '55000', 'spot_at': entry_at.isoformat()}},
        {'recorded_at': exit_at.isoformat(), 'market_snapshot': {'candidates': [exit_contract], 'spot': '55000', 'spot_at': exit_at.isoformat()}},
    ]
    compared = unbiased_baselines({'events': [scan], 'calls': [call], 'market_tape': tape})
    policies = {row['policy']: row for row in compared['scenarios'][0]['policies']}
    assert compared['status'] == 'pass'
    assert policies['hold']['trades'] == 0
    assert policies['deterministic']['trades'] == 1
    assert policies['ai']['trades'] == 1
    assert policies['deterministic']['expectancy'] == policies['ai']['expectancy']
    assert compared['future_quotes_used_before_decision'] is False


def test_fee_notes_stay_redacted_and_incomplete_without_components():
    with pytest.raises(ValueError, match='not redacted'):
        parse_contract_notes([{
            'position_id': 'p', 'trading_date': '2026-08-26', 'source_reference': 'note-1',
            'actual_cost': '7', 'pan': 'ABCDE1234F',
        }])
    costs = {field: '1' if field != 'total' else '7' for field in COMPONENTS}
    notes = parse_contract_notes([{
        'position_id': 'p', 'trading_date': '2026-08-26', 'source_reference': 'note-1', **costs,
    }])
    bundle = {'events': [{'kind': 'paper_entry', 'trading_date': '2026-08-26',
                          'detail': {'position': {'id': 'p'}, 'entry_costs': costs}}]}
    matched = fee_reconciliation(bundle, notes)
    assert matched['status'] == 'pass'
    assert matched['ledger_rewritten'] is False
    assert matched['positions'][0]['match_key'].startswith('p|note-1|')
    assert fee_reconciliation(bundle, [])['status'] == 'incomplete'
    total_only = parse_contract_notes([{
        'position_id': 'p', 'trading_date': '2026-08-26', 'source_reference': 'note-1', 'actual_cost': '7',
    }])
    assert fee_reconciliation(bundle, total_only)['status'] == 'incomplete'
    mismatched = parse_contract_notes([{
        'position_id': 'p', 'trading_date': '2026-08-26', 'source_reference': 'note-2', **{
            field: '1' if field != 'total' else '9' for field in COMPONENTS},
    }])
    assert fee_reconciliation(bundle, mismatched)['status'] == 'fail'


def test_release_chain_stays_incomplete_without_forcing_an_entry():
    bundle = {'calls': [], 'events': [], 'reports': [], 'market_tape': [{
        'trading_date': '2026-08-26', 'market_snapshot': {'provenance': {'release': 'abc'}},
    }]}
    result = release_acceptance(bundle, '2026-08-26', 'abc')
    assert result['status'] == 'incomplete'
    assert result['no_setup'] is True
    assert result['forced_entry'] is False
    assert result['live_orders'] == 'disabled'
    assert 'accepted_fill' in result['missing']
    assert result['checksum_sha256'] == release_acceptance(bundle, '2026-08-26', 'abc')['checksum_sha256']
