import importlib.util
import os
import subprocess
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from kiwit.monitoring_schedule import IST, active_monitoring_window, database_checks_due


@pytest.mark.parametrize('stamp,active,due', [
    ('2026-09-21T08:59:00', False, False),
    ('2026-09-21T09:00:00', True, True),
    ('2026-09-21T15:25:00', True, True),
    ('2026-09-21T16:59:00', True, True),
    ('2026-09-21T17:00:00', False, True),
    ('2026-09-21T23:15:00', False, False),
    ('2026-09-20T10:15:00', False, False),
    ('2026-09-20T10:00:00', False, True),
    ('2026-10-02T10:15:00', False, False),
    ('2027-01-04T10:15:00', True, True),
])
def test_database_wake_windows(stamp, active, due):
    now = datetime.fromisoformat(stamp).replace(tzinfo=IST)
    assert active_monitoring_window(now) is active
    assert database_checks_due(now) is due


@pytest.mark.parametrize('script,mode', [
    ('run_intraday_worker', None), ('run_banknifty_worker', 'decision'),
    ('run_banknifty_worker', 'observe'), ('run_banknifty_worker', 'supervise'),
    ('run_banknifty_worker', 'reports'),
])
def test_scheduled_offhours_workers_do_not_construct_dependencies(monkeypatch, script, mode):
    spec = importlib.util.spec_from_file_location(script, Path('scripts') / f'{script}.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, 'datetime', SimpleNamespace(now=lambda _: datetime(2026, 9, 20, 10, 15, tzinfo=IST)))
    database = Mock(side_effect=AssertionError('Database must not be constructed'))
    monkeypatch.setattr(module, 'PostgresDatabase', database)
    monkeypatch.setattr(module.GrowwSettings, 'from_env', Mock(side_effect=AssertionError('No broker configuration needed')))
    monkeypatch.delenv('KIWIT_DATABASE_URL', raising=False)
    monkeypatch.setattr('sys.argv', [script, '--scheduled'] + (['--mode', mode] if mode else []))
    module.main()
    database.assert_not_called()


@pytest.mark.parametrize('policy,ready', [('deferred', False), ('due', True)])
def test_watchdog_only_checks_database_when_due(tmp_path, policy, ready):
    log = tmp_path / 'calls'
    script = Path('scripts/health_watchdog.sh').read_text()
    script = script.replace('/opt/kiwit/current/.venv/bin/python -m kiwit.monitoring_schedule', f'printf {policy}')
    script = script.replace('/opt/kiwit/current/.venv/bin/python /opt/kiwit/current/scripts/banknifty_health.py',
                            'echo DIAGNOSTICS')
    stubs = 'curl() { echo "$*" >> "$WATCHDOG_TEST_CALLS"; }; logger() { :; };\n'
    result = subprocess.run(['bash', '-c', stubs + script], capture_output=True, text=True,
                            env={**os.environ, 'WATCHDOG_TEST_CALLS': str(log)}, check=True)
    assert '/live' in log.read_text()
    assert ('/ready' in log.read_text()) is ready
    assert ('DIAGNOSTICS' in result.stdout) is ready


@pytest.mark.parametrize('args,stamp', [
    (['--scheduled'], '2026-09-21T15:25:00'),
    (['--scheduled', '--mode', 'reports'], '2026-09-20T23:00:00'),
    ([], '2026-09-20T23:15:00'),
])
def test_market_hourly_and_manual_runs_still_reach_database(monkeypatch, args, stamp):
    spec = importlib.util.spec_from_file_location('worker', 'scripts/run_banknifty_worker.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    now = datetime.fromisoformat(stamp).replace(tzinfo=IST)
    monkeypatch.setattr(module, 'datetime', SimpleNamespace(now=lambda _: now))
    monkeypatch.setattr(module.DatabaseSettings, 'from_env', Mock(side_effect=RuntimeError('database reached')))
    monkeypatch.setattr('sys.argv', ['worker', *args])
    with pytest.raises(RuntimeError, match='database reached'):
        module.main()
