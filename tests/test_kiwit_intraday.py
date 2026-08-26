from datetime import UTC, datetime
from contextlib import contextmanager
from datetime import timedelta
from uuid import uuid4

import pytest

from kiwit.intraday import IntradayService, SignalMailer, _quote_time


def test_intraday_window_is_explicitly_bounded_in_ist() -> None:
    assert IntradayService._window_state(datetime(2026, 8, 26, 4, 0, tzinfo=UTC)) == "entry_window"  # 09:30 IST
    assert IntradayService._window_state(datetime(2026, 8, 26, 10, 1, tzinfo=UTC)) == "reconciliation_window"
    assert IntradayService._window_state(datetime(2026, 8, 26, 10, 20, tzinfo=UTC)) == "closed"
    assert IntradayService._window_state(datetime(2026, 8, 29, 5, 0, tzinfo=UTC)) == "closed"


def test_quote_timestamp_accepts_epoch_milliseconds() -> None:
    now = datetime(2026, 8, 26, 5, 0, tzinfo=UTC)
    assert _quote_time({"timestamp": 1_787_720_400_000}, now) == datetime.fromtimestamp(1_787_720_400, UTC)


def test_mailer_fails_closed_when_not_configured(monkeypatch) -> None:
    for name in ("KIWIT_SMTP_HOST", "KIWIT_EMAIL_FROM", "KIWIT_SMTP_USERNAME", "KIWIT_ALERT_EMAIL"):
        monkeypatch.delenv(name, raising=False)
    mailer = SignalMailer()
    status, detail = mailer.send_signal({}, "/dashboard")
    assert status == "not_configured"
    assert "not configured" in detail


def test_intraday_approval_obeys_dashboard_halt_before_any_fill():
    now = datetime.now(UTC)

    class Connection:
        def __init__(self):
            self.statements = []

        def execute(self, sql, params=None):
            self.statements.append(sql)
            self.sql = sql
            return self

        def fetchone(self):
            if 'FROM intraday_signals' in self.sql:
                return ('NIFTYBEES', now + timedelta(minutes=5), 100, 1, 'pending')
            if 'FROM system_halts' in self.sql:
                return (True,)
            raise AssertionError('Entry progressed beyond halt check')

    connection = Connection()

    class Database:
        @contextmanager
        def transaction(self):
            yield connection

    service = IntradayService(Database(), None)
    with pytest.raises(ValueError, match='entries are halted'):
        service.review(uuid4(), True, 'test-operator', 'test', now)
    assert 'LOCK TABLE system_halts IN SHARE MODE' in connection.statements
    assert not any(sql.startswith(('INSERT', 'UPDATE')) for sql in connection.statements)
