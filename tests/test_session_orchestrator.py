from datetime import UTC, date, datetime, timedelta

import pytest

from kiwit.marketdata.canonical import Session, TradingCalendar
from kiwit.session_orchestrator import REQUIRED, SessionOrchestrator, SessionSchedule

DAY = date(2026, 9, 11)
OPEN = datetime(2026, 9, 11, 3, 45, tzinfo=UTC)
CALENDAR = TradingCalendar("fixture", (Session(DAY),))


class Backend:
    def __init__(self):
        self.healthy, self.closed, self.warm_ok = True, True, True
        self.observations, self.warms, self.reports = [], 0, 0

    def health(self, now):
        return {"as_of": now, "checks": dict.fromkeys(REQUIRED, self.healthy)}

    def warm(self, session_id):
        self.warms += 1
        return self.warm_ok

    def observe(self, event_id, now, *, entries_allowed):
        self.observations.append((event_id, entries_allowed))

    def close(self, session_id, now, *, policy):
        return self.closed

    def report(self, session_id):
        self.reports += 1
        return {"session_id": session_id, "unresolved": 0}


def build(tmp_path, backend=None):
    return SessionOrchestrator(tmp_path / "session.db", calendar=CALENDAR, backend=backend or Backend())


def test_manual_run_cutoff_close_and_report_once(tmp_path):
    core = build(tmp_path)
    assert core.tick(OPEN - timedelta(minutes=20))["state"] == "WAITING"
    core.control(now=OPEN - timedelta(minutes=10), action="RUN", operator="operator")
    assert core.tick(OPEN - timedelta(minutes=5))["state"] == "READY"
    assert core.tick(OPEN)["entries_allowed"] is True
    assert core.tick(OPEN + timedelta(hours=6))["entries_allowed"] is False
    assert core.tick(OPEN + timedelta(hours=6, minutes=15))["state"] == "COMPLETE"
    assert core.tick(OPEN + timedelta(hours=7))["state"] == "COMPLETE"
    assert core.backend.reports == 1


def test_restart_requires_rewarm_and_manual_rearm(tmp_path):
    backend = Backend()
    first = build(tmp_path, backend)
    first.control(now=OPEN, action="RUN", operator="operator")
    assert first.tick(OPEN)["entries_allowed"] is True
    second = build(tmp_path, backend)
    assert second.tick(OPEN + timedelta(minutes=1))["entries_allowed"] is False
    assert backend.warms == 2
    second.control(now=OPEN + timedelta(minutes=2), action="RUN", operator="operator")
    assert second.tick(OPEN + timedelta(minutes=2))["entries_allowed"] is True
    second.control(now=OPEN + timedelta(minutes=3), action="STOP", operator="operator")
    assert second.tick(OPEN + timedelta(minutes=3))["entries_allowed"] is False


def test_dependencies_and_warmup_fail_closed(tmp_path):
    core = build(tmp_path)
    core.control(now=OPEN, action="RUN", operator="operator")
    core.backend.healthy = False
    assert core.tick(OPEN)["state"] == "BLOCKED_DEPENDENCIES"
    assert core.backend.observations[-1][1] is False
    core.backend.healthy = True
    core.backend.warm_ok = False
    assert core.tick(OPEN + timedelta(minutes=1))["state"] == "ERROR"
    core.backend.warm_ok = True
    assert core.tick(OPEN + timedelta(minutes=2))["entries_allowed"] is False


def test_unfinished_previous_day_reconciles_on_holiday(tmp_path):
    core = build(tmp_path)
    core.backend.closed = False
    assert core.tick(OPEN + timedelta(hours=7))["state"] == "CLOSING"
    assert core.tick(OPEN + timedelta(days=1))["state"] == "CLOSING"
    core.backend.closed = True
    assert core.tick(OPEN + timedelta(days=1, minutes=1))["state"] == "CLOSED_CALENDAR"
    assert core.backend.reports == 1


def test_invalid_schedule_unknown_day_and_config_change(tmp_path):
    with pytest.raises(ValueError):
        SessionSchedule(timezone="UTC")
    core = build(tmp_path)
    with pytest.raises(ValueError):
        core.control(now=OPEN - timedelta(days=1), action="RUN", operator="operator")
    core.tick(OPEN)
    changed = SessionOrchestrator(
        tmp_path / "session.db",
        calendar=CALENDAR,
        backend=Backend(),
        schedule=SessionSchedule(entry_cutoff_seconds=600),
    )
    with pytest.raises(ValueError, match="config/calendar"):
        changed.tick(OPEN + timedelta(minutes=1))
