from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from kiwit import session_adapter

NOW = datetime(2026, 9, 11, 4, 30, tzinfo=UTC)


def test_blocked_observation_cannot_create_or_approve_entries():
    service = Mock(settings=SimpleNamespace(symbols=("ABC",)))
    backend = session_adapter.IntradaySessionBackend(service)
    backend.observe("event-1", NOW, entries_allowed=False)
    service._create_signal.assert_not_called()
    service.session_tick.assert_called_once_with(NOW, entries_allowed=False)
    assert service._lifecycle_entry_allowed is False


def test_allowed_observation_clears_permission_on_failure():
    service = Mock(settings=SimpleNamespace(symbols=("ABC",)))
    service._create_signal.side_effect = RuntimeError("failure")
    backend = session_adapter.IntradaySessionBackend(service)
    with pytest.raises(RuntimeError):
        backend.observe("event-2", NOW, entries_allowed=True)
    assert service._lifecycle_entry_allowed is False


def test_run_readiness_cannot_be_bypassed(monkeypatch):
    monkeypatch.setenv("KIWIT_SESSION_CONFIG", "configured")
    core = Mock()
    core._healthy.return_value = False
    monkeypatch.setattr(session_adapter, "orchestrator", lambda _: core)
    with pytest.raises(ValueError, match="dependencies"):
        session_adapter.control(object(), "RUN", "operator", NOW)
    core.control.assert_not_called()


def test_existing_worker_routes_to_lifecycle(monkeypatch):
    from kiwit.intraday import IntradayService

    monkeypatch.setenv("KIWIT_SESSION_CONFIG", "configured")
    callback = Mock(return_value={"state": "OBSERVING_NO_ENTRIES", "entries_allowed": False})
    monkeypatch.setattr(session_adapter, "run", callback)
    service = object.__new__(IntradayService)
    result = IntradayService.run_once.__wrapped__(service, NOW)
    assert result["entries_allowed"] is False
    callback.assert_called_once_with(service, NOW)
