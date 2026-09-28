from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from kiwit.deployment_gate import DeploymentDeferred, quiesce


class Database:
    def __init__(self, exposure=(False, False, False), fail=False):
        self.exposure = exposure
        self.fail = fail
        self.locked = False
        self.queries = []

    @contextmanager
    def transaction(self):
        try:
            yield self
        finally:
            self.locked = False

    def execute(self, sql):
        self.queries.append(sql)
        if sql.startswith('LOCK'):
            if self.fail:
                raise RuntimeError('lock timeout')
            self.locked = True
        return self

    def fetchone(self):
        assert self.locked
        return self.exposure


class System:
    def __init__(self, db, busy=False, failure=None):
        self.db = db
        self.busy = busy
        self.failure = failure
        self.calls = []

    def __call__(self, *args):
        self.calls.append(args)
        if args[0] == 'stop':
            assert self.db.locked
        if args[0] == 'start':
            assert not self.db.locked  # Restore only after transaction exit.
        out = 'active' if args[0] == 'is-active' else 'inactive'
        if args[0] == 'show' and self.busy and args[1] != 'kiwit-api':
            out = 'activating'
        return SimpleNamespace(stdout=out, returncode=int(args == self.failure))


@pytest.mark.parametrize('exposure', [(True, False, False), (False, True, False), (False, False, True)])
def test_open_position_leaves_supervision_and_api_untouched(exposure):
    db = Database(exposure)
    system = System(db)
    with pytest.raises(DeploymentDeferred, match='exposure'):
        quiesce(db, ['decision', 'supervisor'], control=system)
    assert system.calls == []
    assert not db.locked


def test_flat_barrier_holds_locks_through_drain():
    db = Database()
    system = System(db)
    quiesce(db, ['decision', 'supervisor'], control=system)
    assert ('stop', 'supervisor.timer') in system.calls
    assert system.calls[-1] == ('show', 'kiwit-api', '-p', 'ActiveState', '--value')
    assert not db.locked


def test_entry_waiting_on_barrier_aborts_without_killing_worker():
    db = Database()
    system = System(db, busy=True)
    with pytest.raises(DeploymentDeferred, match='drain'):
        quiesce(db, ['decision', 'supervisor'], control=system, timeout=0)
    assert ('start', 'supervisor.timer') in system.calls
    assert ('start', 'kiwit-api') in system.calls
    assert not any(c[0] == 'stop' and c[1].endswith('.service') for c in system.calls)


def test_lock_timeout_never_pauses_workers():
    db = Database(fail=True)
    system = System(db)
    with pytest.raises(RuntimeError, match='lock timeout'):
        quiesce(db, ['supervisor'], control=system)
    assert system.calls == []


def test_systemd_failure_restores_original_scheduling():
    db = Database()
    system = System(db, failure=('stop', 'supervisor.timer'))
    with pytest.raises(RuntimeError, match='systemctl'):
        quiesce(db, ['decision', 'supervisor'], control=system)
    assert ('start', 'supervisor.timer') in system.calls
