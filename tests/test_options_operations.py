# Fixtures are imported from the existing PostgreSQL suite.
# ruff: noqa: F811
import io
import json
from datetime import timedelta
from unittest.mock import patch
from urllib.error import HTTPError

import pytest
from test_banknifty import NOW, Analyst, BankNiftyService, Mailer, Market, db, desk, warm  # noqa: F401
from test_chart_analysis import NOW as CHART_NOW
from test_chart_analysis import payload, rows

from kiwit.chart_analysis import history_context
from kiwit.options_ai import AIFailure, OpenAIPaperAnalyst
from kiwit.options_calendar import regular_session


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
        result['oversized'] = 'x' * 25000
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
