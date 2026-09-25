# Fixtures are imported from the existing PostgreSQL suite.
# ruff: noqa: F811
import io
import json
from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import patch
from urllib.error import HTTPError

import pytest
from test_banknifty import NOW, Analyst, BankNiftyService, Mailer, Market, db, desk, warm  # noqa: F401
from test_chart_analysis import NOW as CHART_NOW
from test_chart_analysis import payload, rows

from kiwit.chart_analysis import history_context
from kiwit.intraday import IST, SignalMailer
from kiwit.options_ai import AIFailure, OpenAIPaperAnalyst
from kiwit.options_calendar import regular_session
from kiwit.options_operations import decision_reason_codes


def test_decision_reason_codes_grace_and_fail_closed_heartbeat():
    open_tick = datetime(2026, 9, 21, 9, 30, 20, tzinfo=IST)
    later = datetime(2026, 9, 21, 10, 0, tzinfo=IST)
    assert decision_reason_codes(None, running=True, local=open_tick) == []
    assert decision_reason_codes(None, running=True, local=later) == ["DECISION_HEARTBEAT_STALE"]
    assert decision_reason_codes({"status": "ok", "age_seconds": 30}, running=True, local=later) == []
    assert decision_reason_codes({"status": "failed", "age_seconds": 15}, running=True, local=later) == [
        "DECISION_WORKER_FAILED"
    ]
    assert decision_reason_codes(
        {"status": "ok", "age_seconds": 15, "detail": {"reason": "entry_rejected"}},
        running=True,
        local=later,
    ) == []
    assert decision_reason_codes({"status": "failed", "age_seconds": 10}, running=True, local=open_tick) == [
        "DECISION_WORKER_FAILED"
    ]


def test_ai_failure_mail_includes_safe_category():
    sent = []
    mailer = SignalMailer()
    mailer.host = "smtp.example"
    mailer.sender = "alerts@example.test"
    mailer.recipients = ("ops@example.test",)
    mailer._send = lambda message: sent.append(message.get_content()) or ("sent", "")
    mailer.send_ai_failure(
        occurred_at=datetime(2026, 9, 21, 10, 10, tzinfo=IST),
        call_id="1edd40e8-ef91-4a0d-8765-5a312cb95ae0",
        dashboard_url="https://kiwit.example/dashboard",
        category="response_validation",
    )
    assert "response_validation" in sent[0]
    assert "unavailable or returned an incomplete result" not in sent[0]
    mailer.send_ai_failure(
        occurred_at=datetime(2026, 9, 21, 10, 10, tzinfo=IST),
        call_id="x",
        dashboard_url="https://kiwit.example/dashboard",
        category="drop table;",
    )
    assert "unspecified" in sent[1]
    assert "drop table" not in sent[1]


def test_verified_holiday_week_is_complete_and_missing_session_still_blocks():
    now = CHART_NOW.replace(month=9, day=21)
    days = ['2026-09-15', '2026-09-16', '2026-09-17', '2026-09-18']
    context = history_context(payload([r for d in days for r in rows(d)]), now)
    coverage = context['previous_calendar_week']['coverage']
    assert coverage['status'] == 'complete'
    assert coverage['scheduled_closures'] == ['2026-09-14']
    assert len(coverage['expected_sessions']) == 4
    missing = history_context(payload([r for d in days[:-1] for r in rows(d)]), now)
    assert missing['previous_calendar_week']['coverage']['status'] == 'incomplete'
    assert regular_session(now.date().replace(year=2027)) is None


def test_provider_failure_is_structured_and_does_not_leak_body(monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY', 'private-key')
    error = HTTPError('https://api.openai.com/v1/responses', 401, 'secret-message',
                      {'x-request-id': 'req_safe123'}, io.BytesIO(b'private-key-secret-body'))
    with patch('kiwit.options_ai.urllib.request.build_opener') as opener:
        opener.return_value.open.side_effect = error
        with pytest.raises(AIFailure) as failure:
            OpenAIPaperAnalyst().decide({'spot': '55000'})
    assert failure.value.evidence['category'] == 'authentication'
    assert failure.value.evidence['http_status'] == 401
    assert failure.value.evidence['request_id'] == 'req_safe123'
    assert failure.value.evidence['dispatched'] is True
    assert 'secret' not in json.dumps(failure.value.evidence)
    assert 'private-key' not in str(failure.value)


def test_local_preflight_failure_does_not_reserve_or_dispatch(desk, monkeypatch):
    service, market, _, _clock = desk
    monkeypatch.setenv('OPENAI_API_KEY', 'test-key')
    service.analyst = OpenAIPaperAnalyst()
    original = market.snapshot
    def oversized(now):
        result = original(now)
        analysis = dict(result.get('chart_analysis') or {})
        analysis['blob'] = 'x' * 30000
        result['chart_analysis'] = analysis
        return result
    market.snapshot = oversized
    with patch('kiwit.options_ai.urllib.request.build_opener') as network:
        assert warm(desk)['position'] is None
        assert network.call_count == 0
    with service.store.locked() as connection:
        assert connection.execute('SELECT count(*) FROM banknifty_ai_calls').fetchone()[0] == 0


def test_failure_evidence_persisted_and_circuit_reopens(desk):
    service, _, analyst, clock = desk
    service.mailer = Mailer()
    analyst.fail = True
    warm(desk)
    for _ in range(16):
        clock[0] += timedelta(minutes=1)
        service.run_once()
    assert analyst.calls == 3
    with service.store.locked() as connection:
        failures = connection.execute('SELECT result FROM banknifty_ai_calls').fetchall()
    assert all(r[0]['failure']['category'] == 'timeout' for r in failures)
    assert all(r[0]['alert_delivery'] == 'sent' for r in failures)
    for _ in range(5):
        clock[0] += timedelta(minutes=1)
        service.run_once()
    assert analyst.calls == 4


def test_overdue_trade_excluded_from_learning_and_intraday_review(desk):
    service, _, _, clock = desk
    warm(desk)
    clock[0] += timedelta(days=1)
    service.supervise()
    status = service.status()
    assert status['operations']['recovery_trades'] == 1
    assert status['paper_review'] == []
    assert status['learning']['playbook_evidence'] == []
    exit_event = next(e['detail'] for e in status['events'] if e['kind'] == 'paper_exit')
    assert exit_event['recovery'] is True
    assert exit_event['holding_seconds'] >= 86400


def test_reports_catch_up_without_consuming_attempts_when_unconfigured(desk):
    service, _, _, clock = desk
    warm(desk)
    service.mailer = Mailer()
    service.mailer.configured = False
    clock[0] += timedelta(days=1)
    service._process_daily_report(None, clock[0])
    report = service.status()['daily_reports'][0]
    assert report['generated_late'] is True
    assert report['delivery']['attempts'] == 0
    service.mailer.configured = True
    service._process_daily_report(None, clock[0])
    assert len(service.mailer.reports) == 1
    service._process_daily_report(None, clock[0])
    assert len(service.mailer.reports) == 1


def test_report_claim_prevents_nested_sender(desk):
    service, _, _, clock = desk
    warm(desk)
    clock[0] = NOW.replace(hour=10, minute=0)
    class NestedMailer(Mailer):
        def send_daily_report(self, report, url):
            service._process_daily_report(None, clock[0])
            return super().send_daily_report(report, url)
    service.mailer = NestedMailer()
    service._process_daily_report(None, clock[0])
    assert len(service.mailer.reports) == 1


def test_observer_without_run_cannot_create_ai_call_or_position(db):
    market, analyst = Market(), Analyst()
    service = BankNiftyService(db, None, market=market, analyst=analyst, clock=lambda: NOW)
    assert service.observe()['state'] == 'observed'
    with db.transaction() as connection:
        assert connection.execute('SELECT count(*) FROM banknifty_sessions').fetchone()[0] == 0
        assert connection.execute('SELECT count(*) FROM banknifty_ai_calls').fetchone()[0] == 0
        assert connection.execute('SELECT count(*) FROM banknifty_market_history').fetchone()[0] == 1
    assert analyst.calls == 0


def _snapshot_write_fixtures():
    observer_snapshot = {
        'spot_at': NOW.isoformat(),
        'spot': '55000',
        'candidates': [{'symbol': 'BANKNIFTY26SEP55000CE', 'quote': {'bid': '100', 'ask': '101'}}],
        'chart_cache': {'daily': [{'at': NOW.isoformat(), 'close': '55000'}]},
        'provenance': {'release': 'test'},
    }
    selection = {
        'version': 'selector-test',
        'plans': [{'id': 'plan-1', 'symbol': 'BANKNIFTY26SEP55000CE'}],
        'evaluations': [{'playbook': 'range_reversal', 'eligible': True}],
        'mode': 'executable',
    }
    decision_snapshot = {
        key: value for key, value in observer_snapshot.items() if key != 'chart_cache'
    }
    decision_snapshot.update({
        'event_context': {'coverage': 'configured', 'risk': 'clear'},
        'strategy_selection': selection,
        'entry_gate': {'allowed': True, 'reason_codes': []},
        'decision_event_key': 'decision-1',
    })
    return observer_snapshot, decision_snapshot, selection


def _assert_enriched_market_snapshot(connection):
    rows = connection.execute(
        'SELECT recorded_at,market_snapshot,strategy_selection,scan_state '
        'FROM banknifty_market_history'
    ).fetchall()
    assert len(rows) == 1
    recorded_at, snapshot, selection, scan_state = rows[0]
    assert snapshot['candidates'][0]['symbol'] == 'BANKNIFTY26SEP55000CE'
    assert snapshot['chart_cache']['daily'][0]['close'] == '55000'
    assert snapshot['event_context'] == {'coverage': 'configured', 'risk': 'clear'}
    assert snapshot['entry_gate'] == {'allowed': True, 'reason_codes': []}
    assert snapshot['decision_event_key'] == 'decision-1'
    assert selection['plans'][0]['id'] == 'plan-1'
    assert selection['evaluations'][0]['playbook'] == 'range_reversal'
    assert scan_state == 'live_observation'
    return recorded_at


def test_observer_first_snapshot_is_enriched_and_decision_retry_is_idempotent(db):
    service = BankNiftyService(db, None, market=Market(), analyst=Analyst(), clock=lambda: NOW)
    observer, decision, selection = _snapshot_write_fixtures()
    day = str(NOW.astimezone(IST).date())
    with service.store.locked() as connection:
        service.store.record_market_snapshot(connection, {'day': day, 'detail': 'live_observation'},
                                             observer, {'plans': [], 'evaluations': [],
                                                        'mode': 'observation_only'})
        service.store.record_market_snapshot(connection, {'day': day, 'detail': 'Scanning Bank Nifty'},
                                             decision, selection)
        first_recorded_at = _assert_enriched_market_snapshot(connection)
        service.store.record_market_snapshot(connection, {'day': day, 'detail': 'Scanning Bank Nifty'},
                                             decision, selection)
        assert _assert_enriched_market_snapshot(connection) == first_recorded_at


def test_decision_first_snapshot_survives_later_observer_write(db):
    service = BankNiftyService(db, None, market=Market(), analyst=Analyst(), clock=lambda: NOW)
    observer, decision, selection = _snapshot_write_fixtures()
    day = str(NOW.astimezone(IST).date())
    with service.store.locked() as connection:
        service.store.record_market_snapshot(connection, {'day': day, 'detail': 'Scanning Bank Nifty'},
                                             decision, selection)
        service.store.record_market_snapshot(connection, {'day': day, 'detail': 'live_observation'},
                                             observer, {'plans': [], 'evaluations': [],
                                                        'mode': 'observation_only'})
        _assert_enriched_market_snapshot(connection)


def test_frozen_selector_replays_without_model_or_future_data():
    from test_playbooks import fixtures

    from kiwit.options_replay import replay
    snapshot, state, _ = fixtures()
    snapshot.update(capital=state['amount'], cash=state['cash'], loss_pct=state['loss_pct'],
                    profit_pct=state['profit_pct'], day=state['day'], realized_pnl='0', provenance={'release': 'test'})
    bundle = {'calls': [{'call_id': 'fixture', 'snapshot': snapshot}]}
    assert replay(bundle)['matching'] == 1
    snapshot['strategy_selection']['plans'][0]['id'] = 'tampered'
    assert replay(bundle)['mismatches'] == 1


def test_replay_rejects_tampered_plan_contents():
    from test_playbooks import fixtures

    from kiwit.options_replay import replay
    snapshot, state, _ = fixtures()
    snapshot.update(capital=state['amount'], cash=state['cash'], loss_pct=state['loss_pct'],
                    profit_pct=state['profit_pct'], day=state['day'], realized_pnl='0', provenance={'release': 'test'})
    snapshot['strategy_selection']['plans'][0]['quantity'] = 999999
    assert replay({'calls': [{'call_id': 'fixture', 'snapshot': snapshot}]})['mismatches'] == 1


def test_report_failed_delivery_retries_after_backoff(desk):
    service, _, _, clock = desk
    warm(desk)
    class FailingMailer(Mailer):
        def send_daily_report(self, report, url):
            self.reports.append(report)
            return ('failed', 'transport') if len(self.reports) == 1 else ('sent', '')
    service.mailer = FailingMailer()
    clock[0] = NOW.replace(hour=10, minute=0)
    service._process_daily_report(None, clock[0])
    service._process_daily_report(None, clock[0] + timedelta(minutes=1))
    assert len(service.mailer.reports) == 1
    service._process_daily_report(None, clock[0] + timedelta(minutes=15))
    assert len(service.mailer.reports) == 2
    assert service.status()['daily_reports'][0]['delivery']['status'] == 'sent'


def test_report_failure_does_not_starve_later_unattempted_report(desk):
    service, _, _, _clock = desk
    warm(desk)
    with service.store.locked() as connection:
        for day in ('2026-08-24', '2026-08-25'):
            connection.execute(
                "INSERT INTO banknifty_daily_reports(trading_date,generated_at,report) VALUES(%s,%s,%s::jsonb)",
                (day, NOW, json.dumps({'day': day})),
            )
    class FailOldest(Mailer):
        def send_daily_report(self, report, _url):
            self.reports.append(report['day'])
            return ('failed', 'transport') if report['day'] == '2026-08-24' else ('sent', '')
    service.mailer = FailOldest()
    service._process_daily_report(None, NOW)
    service._process_daily_report(None, NOW + timedelta(minutes=15))
    assert service.mailer.reports == ['2026-08-24', '2026-08-25']


def test_runtime_role_can_write_but_cannot_drop_tables(db):
    import importlib.util
    from pathlib import Path

    import psycopg
    spec = importlib.util.spec_from_file_location('roles', Path('scripts/provision_database_roles.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with db.transaction() as connection:
        connection.execute('CREATE TABLE public.kiwit_privilege_fixture(value integer)')
        module.provision(connection, 'isolated-test-password-only', 'isolated-test-reader-only')
        connection.execute('SET LOCAL ROLE kiwit_runtime')
        connection.execute('INSERT INTO public.kiwit_privilege_fixture VALUES(1)')
        assert connection.execute('SELECT value FROM public.kiwit_privilege_fixture').fetchone()[0] == 1
        with pytest.raises(psycopg.errors.InsufficientPrivilege), connection.transaction():
            connection.execute('DROP TABLE public.kiwit_privilege_fixture')
        with pytest.raises(psycopg.errors.InsufficientPrivilege), connection.transaction():
            connection.execute('CREATE TABLE public.kiwit_forbidden(value integer)')
        connection.execute('RESET ROLE')
        connection.execute('SET LOCAL ROLE kiwit_reader')
        assert connection.execute('SELECT count(*) FROM public.kiwit_privilege_fixture').fetchone()[0] == 1
        with pytest.raises(psycopg.errors.InsufficientPrivilege), connection.transaction():
            connection.execute('INSERT INTO public.kiwit_privilege_fixture VALUES(2)')
        connection.execute('RESET ROLE')


def test_expired_residual_requires_settlement_and_never_fabricates_exit(desk):
    service, _, _, clock = desk
    warm(desk)
    clock[0] = NOW.replace(month=9, day=30)
    service.supervise()
    status = service.status()
    assert status['session']['position'] is not None
    assert 'settlement' in status['session']['detail']
    assert not any(e['kind'] == 'paper_exit' for e in status['events'])


def test_verified_expired_settlement_unblocks_the_next_session(desk):
    service, _, _, clock = desk
    initial = warm(desk)
    clock[0] = NOW.replace(month=9, day=30)
    service.supervise()
    position_id = initial['position']['id']
    settled = service.settle_expired_position(position_id, '12.50', 'NSE final settlement bulletin fixture', 'test')
    assert settled['state'] == 'completed' and settled['position'] is None
    event = next(e for e in service.status()['events'] if e['kind'] == 'paper_settlement')
    assert event['detail']['source_reference'] == 'NSE final settlement bulletin fixture'
    assert service.status()['operations']['recovery_trades'] == 1
    assert service.status()['learning']['playbook_evidence'] == []
    assert service.settle_expired_position(position_id, '12.50', 'NSE final settlement bulletin fixture', 'test')['state'] == 'already_settled'
    with pytest.raises(ValueError, match='different evidence'):
        service.settle_expired_position(position_id, '13', 'NSE final settlement bulletin fixture', 'test')
    next_day = clock[0] + timedelta(days=1)
    clock[0] = next_day
    assert service.start(100000, 5, 10, 'test')['state'] == 'running'


def test_interrupted_calls_are_conservatively_reconciled_and_visible(desk):
    service, market, _, clock = desk
    service.start(100000, 5, 10, 'test')
    call_id = service.store.reserve(clock[0], market.snapshot(clock[0]))
    clock[0] += timedelta(minutes=11)
    service.run_once()
    with service.store.locked() as connection:
        row = connection.execute('SELECT state,result FROM banknifty_ai_calls WHERE call_id=%s', (call_id,)).fetchone()
    assert row[0] == 'interrupted' and row[1]['recovery']['charge_retained'] is True


def test_decision_heartbeat_is_required_during_a_running_session(desk):
    from kiwit.options_operations import diagnostics

    service, _, _, clock = desk
    service.start(100000, 5, 10, 'test')
    service.observe()
    service.supervise()
    assert 'DECISION_HEARTBEAT_STALE' in diagnostics(service.store, clock[0])['reason_codes']


def test_decision_heartbeat_has_grace_at_session_open(desk):
    from kiwit.options_operations import diagnostics

    service, _, _, clock = desk
    clock[0] = NOW.replace(hour=4, minute=0, second=20)
    service.start(100000, 5, 10, 'test')
    service.observe()
    service.supervise()
    assert 'DECISION_HEARTBEAT_STALE' not in diagnostics(service.store, clock[0])['reason_codes']


def test_fail_closed_entry_does_not_page_as_decision_worker_failed(desk):
    from kiwit.options_operations import diagnostics

    service, _, analyst, clock = desk
    original = analyst.decide

    def wrong_contract(snapshot):
        decision, usage = original(snapshot)
        decision['symbol'] = 'NIFTY-FAKE'
        return decision, usage

    analyst.decide = wrong_contract
    assert warm(desk)['position'] is None
    codes = diagnostics(service.store, clock[0])['reason_codes']
    assert 'DECISION_WORKER_FAILED' not in codes


def test_rolling_budget_renews_without_erasing_historical_charges(desk):
    service, market, _, clock = desk
    service.start(100000, 5, 10, 'test')
    with service.store.locked() as connection:
        connection.execute(
            "INSERT INTO banknifty_ai_calls(call_id,trading_date,slot,state,reserved_usd,snapshot) "
            "VALUES(gen_random_uuid(),%s,1,'interrupted',50,'{}'::jsonb)",
            (clock[0].date() - timedelta(days=30),))
    call = service.store.reserve(clock[0], market.snapshot(clock[0]))
    assert call is not None
    status = service.status()['budget']
    assert Decimal(status['rolling_used_or_reserved_usd']) == Decimal('.20')
    with service.store.locked() as connection:
        connection.execute("UPDATE banknifty_ai_calls SET trading_date=%s WHERE slot=1",
                           (clock[0].date() - timedelta(days=29),))
    clock[0] += timedelta(minutes=2)
    assert service.store.reserve(clock[0], market.snapshot(clock[0])) is None


def test_report_worker_recovers_abandoned_calls_and_preserves_charge_warning(desk):
    service, market, _, clock = desk
    service.start(100000, 5, 10, 'test')
    call = service.store.reserve(clock[0], market.snapshot(clock[0]))
    clock[0] += timedelta(minutes=11)
    service._process_daily_report(None, clock[0])
    service.store.settle(call, {'action': 'HOLD'}, {'budget_charge_usd': '0'}, now=clock[0])
    operations = service.status()['operations']
    assert operations['interrupted_ai_calls'] == 1
    assert 'INTERRUPTED_AI_CHARGES_RETAINED_CHECK_PROVIDER_USAGE' in operations['security_notices']


def test_partial_exit_then_expiry_settlement_reconciles_all_realized_pnl(desk):
    service, market, _, clock = desk
    initial = warm(desk)
    service.stop('test')
    market.size = 30
    clock[0] += timedelta(minutes=1)
    service.run_once()
    assert service.status()['session']['position'] is not None
    clock[0] = NOW.replace(month=9, day=30)
    with pytest.raises(ValueError, match='no longer matches'):
        service.settle_expired_position('wrong-position', '12', 'fixture bulletin', 'test')
    service.settle_expired_position(initial['position']['id'], '12', 'fixture bulletin', 'test', settlement_fees='1.25')
    status = service.status()
    assert Decimal(status['operations']['recovery_pnl']) == Decimal(status['session']['realized_pnl'])
    assert status['operations']['recovery_trades'] == 1
    assert status['paper_review'] == []
    assert status['learning']['playbook_evidence'] == []


def test_frozen_entry_and_partial_exit_replay_detects_changed_fill(desk):
    from kiwit.options_replay import replay

    service, market, _, clock = desk
    warm(desk)
    service.stop('test')
    market.size = 30
    clock[0] += timedelta(minutes=1)
    service.run_once()
    with service.store.locked() as connection:
        calls = connection.execute('SELECT call_id,snapshot,result FROM banknifty_ai_calls ORDER BY slot').fetchall()
        events = connection.execute('SELECT event_id,kind,detail FROM banknifty_events ORDER BY event_id').fetchall()
    bundle = {'calls': [dict(zip(('call_id', 'snapshot', 'result'), row, strict=True)) for row in calls],
              'events': [dict(zip(('event_id', 'kind', 'detail'), row, strict=True)) for row in events]}
    result = replay(bundle)
    assert len(result['fills']) == 2
    assert all(fill['status'] == 'match' for fill in result['fills'])
    entry = next(e for e in bundle['events'] if e['kind'] == 'paper_entry')
    entry['detail']['position']['entry'] = '999'
    assert replay(bundle)['fills'][0]['status'] == 'mismatch'
