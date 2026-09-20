"""Database-free scheduling for automatic monitoring and worker invocations."""
from datetime import UTC, datetime, time
from zoneinfo import ZoneInfo

from .options_calendar import regular_session

IST = ZoneInfo('Asia/Kolkata')


def active_monitoring_window(now):
    local = now.astimezone(IST)
    # Unknown calendar years retain weekday monitoring; this never authorizes trades.
    return (local.weekday() < 5 and regular_session(local.date()) is not False
            and time(9) <= local.time() < time(17))


def database_checks_due(now):
    return active_monitoring_window(now) or now.astimezone(IST).minute == 0


def scheduled_worker_due(mode, now):
    return database_checks_due(now) if mode == 'reports' else active_monitoring_window(now)


if __name__ == '__main__':
    print('due' if database_checks_due(datetime.now(UTC)) else 'deferred')
