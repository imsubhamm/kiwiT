import json
from copy import deepcopy
from datetime import timedelta
from unittest.mock import MagicMock

import pytest
from test_options_evaluation import evidence
from test_playbooks import NOW

from kiwit.banknifty import BankNiftyStore
from kiwit.options_evaluation import counterfactual
from kiwit.options_retention import TRACKING_CLASSES, retention_window, tracking_capacity, tracking_coverage
from kiwit.playbooks import fingerprint


@pytest.mark.parametrize('reason', TRACKING_CLASSES)
def test_all_classes_cover_delayed_entry_max_horizon_and_grace(reason):
    connection = MagicMock()
    BankNiftyStore(None).track_plans(connection, {'day': str(NOW.date())},
                                  [{'symbol': 'OLD', 'kind': 'CE'}], [], NOW, reason=reason)
    args = connection.execute.call_args.args[1]
    assert args[5] == NOW + timedelta(minutes=63)
    assert json.loads(args[6]) == [reason]


def test_opened_position_longer_retention_is_preserved():
    connection = MagicMock()
    BankNiftyStore(None).track_plans(connection, {'day': str(NOW.date())},
                                  [{'symbol': 'OPEN', 'kind': 'CE'}], [], NOW,
                                  minutes=360, reason='opened_position')
    assert connection.execute.call_args.args[1][5] == NOW + timedelta(hours=6)


def test_capacity_is_deterministic_and_reports_every_class_and_exclusion():
    rows = [(f'S{i:02}', list(TRACKING_CLASSES[:-2]), NOW) for i in range(65)]
    rows.append(('OPEN', ['opened_position', 'calendar_blocked_shadow'], NOW + timedelta(minutes=1)))
    result = tracking_capacity(rows)
    assert result == tracking_capacity(list(reversed(rows)))
    assert result['selected_symbols'][0] == 'OPEN'
    assert result['overflow_symbols'] == ['S63', 'S64']
    assert result['required_count'] == 66
    coverage = tracking_coverage(result, [{'symbol': s, 'quote': {'bid': '100'}}
                                         for s in result['selected_symbols'] if s != 'S00'])
    assert coverage['classes']['ordinary_candidate']['missing_symbols'] == ['S00', 'S63', 'S64']
    assert coverage['classes']['opened_position']['observed_count'] == 1
    assert coverage['classes']['calendar_blocked_shadow']['observed_count'] == 1


def test_retention_survives_universe_departure_through_final_exit_grace():
    _, plan, tape = evidence()
    plan = deepcopy(plan)
    plan['expires_at'] = (NOW + timedelta(minutes=2)).isoformat()
    plan['id'] = fingerprint({k: v for k, v in plan.items() if k != 'id'})
    entry_at, exit_at = NOW + timedelta(seconds=90), NOW + retention_window()
    for row, at in zip(tape, (entry_at, exit_at)):
        row['recorded_at'] = at.isoformat()
        row['market_snapshot']['spot_at'] = at.isoformat()
        candidate = row['market_snapshot']['candidates'][0]
        candidate.update(selection_eligible=False, tracked=True)
        candidate['quote']['stamp'] = at.isoformat()
    result = counterfactual(plan, tape, NOW, 60)
    assert result['status'] == 'observed'
    assert result['exited_at'] == exit_at.isoformat()
    assert counterfactual(plan, tape[:1], NOW, 60)['reason'] == 'future_exit_quote_or_depth_missing'


def test_late_day_horizon_is_retained_but_never_fabricates_after_close_exit():
    from kiwit.intraday import IST
    _, plan, tape = evidence()
    start = NOW.astimezone(IST).replace(hour=15, minute=0, second=0)
    plan = deepcopy(plan)
    plan['created_at'] = start.isoformat()
    plan['expires_at'] = (start + timedelta(minutes=2)).isoformat()
    plan['id'] = fingerprint({k: v for k, v in plan.items() if k != 'id'})
    for row, at in zip(tape, (start, start + timedelta(minutes=60))):
        row['recorded_at'] = at.isoformat()
        row['market_snapshot']['spot_at'] = at.isoformat()
        row['market_snapshot']['candidates'][0]['quote']['stamp'] = at.isoformat()
    assert counterfactual(plan, tape, start, 60) == {
        'status': 'excluded', 'reason': 'future_exit_quote_or_depth_missing'}
    assert (start + retention_window()).hour == 16


def test_market_still_fetches_contract_after_it_leaves_nearest_strikes(monkeypatch):
    from types import SimpleNamespace

    from kiwit.options_market import BankNiftyMarket

    bar = {'at': NOW.isoformat(), 'close': 55000, 'open': 55000, 'high': 55001, 'low': 54999}
    monkeypatch.setattr('kiwit.options_market.parse_minutes', lambda *_: [bar])
    monkeypatch.setattr('kiwit.options_market.history_context', lambda *_: {})
    monkeypatch.setattr('kiwit.options_market.analyse', lambda *_: {})
    market = BankNiftyMarket(SimpleNamespace(banknifty_candles=lambda *_: {}), clock=lambda: NOW)
    contracts = [{'symbol': f'S{i}', 'strike': str(55000 + i * 100), 'lot': 30} for i in range(6)]
    market.contracts = lambda _: contracts
    requested = []

    def quote(symbol, _now):
        requested.append(symbol)
        return {'bid': '100', 'ask': '101', 'bid_size': 60, 'ask_size': 60,
                'stamp': NOW.isoformat(), 'market_fields': {}, 'market_field_sources': {}}

    market.quote = quote
    # S5 is outside the five nearest strikes but has a retained obligation.
    snapshot = market.snapshot(NOW, tracked_symbols=['S5'])
    assert 'S5' in requested
    departed = next(c for c in snapshot['candidates'] if c['symbol'] == 'S5')
    assert departed['tracked'] and not departed['selection_eligible']
    requested.clear()
    market.snapshot(NOW)
    assert 'S5' not in requested
    # Contract removal from the instrument catalogue is recorded as missing coverage.
    coverage = tracking_coverage(tracking_capacity([('REMOVED', ['ordinary_candidate'], NOW)]),
                                 snapshot['candidates'])
    assert coverage['classes']['ordinary_candidate']['missing_symbols'] == ['REMOVED']


@pytest.mark.parametrize('action,execution_rejected,expected', [
    ('BUY', False, 'selected_plan'), ('HOLD', False, 'rejected_plan'), ('BUY', True, 'rejected_plan'),
])
def test_decision_loop_tracks_selected_and_rejected_plans(monkeypatch, action, execution_rejected, expected):
    from test_banknifty import NOW as tick
    from test_banknifty import Analyst, BankNiftyService, Market

    monkeypatch.setenv('KIWIT_BANKNIFTY_AI_ENABLED', 'true')
    monkeypatch.setenv('OPENAI_API_KEY', 'test')
    monkeypatch.setattr('kiwit.banknifty.event_context', lambda _: {'coverage': 'configured', 'risk': 'clear'})
    state = {'day': str(tick.date()), 'state': 'running', 'position': None,
             'amount': '100000', 'cash': '100000', 'loss_pct': '5', 'profit_pct': '10',
             'realized_pnl': '0', 'entries': 0,
             'history': [{'at': (tick - timedelta(minutes=i)).isoformat(), 'spot': '55000'}
                         for i in range(4, -1, -1)]}
    analyst = Analyst()
    analyst.action = action
    service = BankNiftyService(None, None, market=Market(), analyst=analyst, clock=lambda: tick)
    service.store = MagicMock()
    service.store.latest.return_value = state
    service.store.learning_context.return_value = {}
    service.store.decision_attempts.return_value = []
    connection = service.store.locked.return_value.__enter__.return_value
    connection.execute.return_value.fetchone.return_value = None
    connection.execute.return_value.fetchall.return_value = []
    service._monitor = lambda: state
    service._process_daily_report = lambda *_: None

    def apply(*_):
        if execution_rejected:
            raise ValueError('Entry validation failed')

    service._apply = apply
    service.run_once()
    assert analyst.calls == 1
    tracked = [call.kwargs.get('reason', 'eligible_plan') for call in service.store.track_plans.call_args_list
               if call.args[2]]
    assert 'ordinary_candidate' in tracked
    assert 'eligible_plan' in tracked
    assert expected in tracked
