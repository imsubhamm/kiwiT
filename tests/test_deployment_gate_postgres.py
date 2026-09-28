"""Real lock/commit ordering tests, run by CI's isolated PostgreSQL service."""
import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Event
from types import SimpleNamespace
from uuid import uuid4

import pytest

from kiwit.deployment_gate import DeploymentDeferred, quiesce


@pytest.fixture
def database():
    url = os.getenv('KIWIT_TEST_DATABASE_URL')
    if not url:
        pytest.skip('requires isolated PostgreSQL')
    import psycopg
    from psycopg import sql

    schema = 'deploy_gate_' + uuid4().hex
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))

    class Database:
        @contextmanager
        def transaction(self):
            with psycopg.connect(url) as conn:
                conn.execute(sql.SQL('SET search_path TO {}').format(sql.Identifier(schema)))
                yield conn

    db = Database()
    try:
        with db.transaction() as conn:
            conn.execute('CREATE TABLE banknifty_sessions (state jsonb NOT NULL)')
            conn.execute('CREATE TABLE paper_positions (quantity numeric NOT NULL)')
            conn.execute('CREATE TABLE intraday_signals (status text NOT NULL)')
        yield db
    finally:
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))


@pytest.mark.parametrize('statement', [
    "INSERT INTO banknifty_sessions VALUES ('{\"position\": {\"quantity\": 15}}')",
    'INSERT INTO paper_positions VALUES (1)',
    "INSERT INTO intraday_signals VALUES ('entered')",
])
def test_open_paper_position_defers_without_stopping_supervision(database, statement):
    with database.transaction() as conn:
        conn.execute(statement)
    calls = []
    with pytest.raises(DeploymentDeferred):
        quiesce(database, ['supervisor'], control=lambda *args: calls.append(args))
    assert calls == []


def test_inflight_entry_commits_before_gate_check_and_defers(database):
    calls = []
    started = Event()

    def gate():
        started.set()
        with pytest.raises(DeploymentDeferred):
            quiesce(database, ['supervisor'], control=lambda *args: calls.append(args))

    with ThreadPoolExecutor() as pool:
        with database.transaction() as writer:
            writer.execute('INSERT INTO paper_positions VALUES (1)')
            result = pool.submit(gate)
            assert started.wait(5)
        result.result(timeout=10)
    assert calls == []


def test_entry_racing_after_flat_check_cannot_commit_until_abort_restores_scheduling(database):
    attempted = Event()
    committed = Event()
    calls = []

    def entry():
        with database.transaction() as writer:
            writer.execute("SET LOCAL lock_timeout = '10s'")
            attempted.set()
            writer.execute('INSERT INTO paper_positions VALUES (1)')
        committed.set()

    with ThreadPoolExecutor() as pool:
        future = None

        def control(*args):
            nonlocal future
            calls.append(args)
            if args == ('stop', 'supervisor.timer'):
                future = pool.submit(entry)
                assert attempted.wait(5)
                assert not committed.wait(0.1)
            out = 'active' if args[0] in {'is-active', 'show'} else ''
            return SimpleNamespace(returncode=0, stdout=out)

        with pytest.raises(DeploymentDeferred, match='drain'):
            quiesce(database, ['supervisor'], control=control, timeout=0)
        future.result(timeout=10)
    assert committed.is_set()
    assert ('start', 'supervisor.timer') in calls
    assert ('start', 'kiwit-api') in calls
