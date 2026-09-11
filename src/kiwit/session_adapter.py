"""EC2 adapter for the existing durable cash-paper backend; opt-in configuration."""

import json
import logging
import os
from contextlib import closing
from datetime import date, datetime, time
from pathlib import Path

from .marketdata.canonical import IST, Session, TradingCalendar
from .ml.datasets import _hash
from .session_orchestrator import SessionOrchestrator, SessionSchedule


def configured():
    return bool(os.getenv("KIWIT_SESSION_CONFIG"))


class IntradaySessionBackend:
    def __init__(self, service):
        self.service = service

    def health(self, now):
        service = self.service
        with service.database.transaction() as connection:
            connection.execute("SELECT 1")
            connection.execute("SELECT 1 FROM intraday_audit_events LIMIT 1")
            rows = connection.execute(
                "SELECT symbol,count(*) FROM intraday_quotes WHERE symbol=ANY(%s) "
                "AND observed_at<=%s AND date(observed_at AT TIME ZONE 'Asia/Kolkata')=%s GROUP BY symbol",
                (list(service.settings.symbols), now, now.astimezone(IST).date()),
            ).fetchall()
            halted = connection.execute(
                "SELECT EXISTS(SELECT 1 FROM system_halts WHERE active AND scope IN ('global',%s))",
                (service.settings.account_id,),
            ).fetchone()[0]
        # Actual read-only quotes validate auth; a configured token alone is not readiness.
        auth = service.broker is not None
        for symbol in service.settings.symbols:
            try:
                service.ingest_quote(symbol, now)
            except Exception:  # noqa: BLE001 - fail readiness without exposing provider credentials
                auth = False
        counts = dict(rows)
        history = bool(service.settings.symbols) and all(counts.get(s, 0) >= 20 for s in service.settings.symbols)
        return {
            "as_of": now,
            "checks": {
                "database": True,
                "audit": True,
                "groww_auth": auth,
                "configuration": service.settings.enabled and not halted,
                "history": history,
                "features": history,
                # This adapter runs the deployed deterministic observer, not V2 specialist inference.
                "models": True,
            },
        }

    def warm(self, session_id):
        # Readiness has validated the 20 same-day observations consumed by _create_signal.
        return True

    def observe(self, event_id, now, *, entries_allowed):
        service = self.service
        service._lifecycle_entry_allowed = entries_allowed
        try:
            service.monitor_exits(now)
            service.session_tick(now, entries_allowed=entries_allowed)
            if entries_allowed:
                for symbol in service.settings.symbols:
                    service._create_signal(symbol, now)
                service.session_tick(now, entries_allowed=True)
        finally:
            service._lifecycle_entry_allowed = False

    def close(self, session_id, now, *, policy):
        service = self.service
        # Refresh quotes for recovery, including an unfinished previous day.
        for symbol in service.settings.symbols:
            try:
                service.ingest_quote(symbol, now)
            except Exception as error:  # noqa: BLE001 - preserve reconciliation on feed failure
                logging.getLogger(__name__).warning("Session close quote unavailable: %s", type(error).__name__)
        with service.database.transaction() as connection:
            session = service._active_session(connection)
            if session:
                if policy == "FLATTEN_ON_FRESH_QUOTE":
                    service._set_session(connection, session[0], "stopping", "Orchestrator session close", now)
                else:
                    return False
        service.session_tick(now, entries_allowed=False)
        with service.database.transaction() as connection:
            remaining = connection.execute(
                "SELECT EXISTS(SELECT 1 FROM intraday_signals WHERE account_id=%s AND status='entered')",
                (service.settings.account_id,),
            ).fetchone()[0]
        return not remaining

    def report(self, session_id):
        with self.service.database.transaction() as connection:
            rows = connection.execute(
                "SELECT trading_date,state,detail FROM paper_sessions WHERE account_id=%s ORDER BY trading_date DESC LIMIT 1",
                (self.service.settings.account_id,),
            ).fetchall()
        return {
            "session_id": session_id,
            "mode": "paper",
            "engine": "legacy-cash-observer",
            "sessions": [[str(value) for value in row] for row in rows],
        }


def orchestrator(service):
    config = json.loads(Path(os.environ["KIWIT_SESSION_CONFIG"]).read_text())
    if config["engine"] != "legacy-cash-observer":
        raise ValueError("This adapter supports only the durable cash-paper observer")
    calendar = TradingCalendar(
        config["calendar_version"],
        tuple(
            Session(date.fromisoformat(s["day"]), time.fromisoformat(s["opens"]), time.fromisoformat(s["closes"]))
            for s in config["sessions"]
        ),
    )
    core = SessionOrchestrator(
        config["state_path"],
        calendar=calendar,
        backend=IntradaySessionBackend(service),
        schedule=SessionSchedule(**config["schedule"]),
    )
    # API/oneshot workers share a consent epoch; reboot or release change revokes prior consent.
    boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    release = os.environ.get("KIWIT_RELEASE_SHA")
    if not release:
        raise ValueError("Release identity required for shared lifecycle consent")
    core.boot_id = _hash({"boot": boot, "release": release})
    return core


def control(service, action, operator, now):
    if configured():
        core = orchestrator(service)
        if action == "STOP":
            with closing(core._db()) as db, db:
                db.execute("BEGIN IMMEDIATE")
                for (body,) in db.execute("SELECT body FROM market_sessions").fetchall():
                    state = json.loads(body)
                    state["armed_boot"] = None
                    state["events"].append({"at": now.isoformat(), "action": "STOP", "operator": operator})
                    core._save(db, state)
            return
        if action == "RUN" and not core._healthy(now):
            raise ValueError("Session dependencies are not ready; history, Groww approval and health are required")
        core.control(now=now, action=action, operator=operator)


def run(service, now: datetime):
    return orchestrator(service).tick(now)


def entry_permitted(service, now):
    if not configured():
        return True
    if getattr(service, "_lifecycle_entry_allowed", False):
        return True
    core = orchestrator(service)
    session = core.calendar.session(now.astimezone(IST).date())
    if session is None:
        return False
    from datetime import timedelta

    opens, closes = session.bounds()
    if (
        not opens + timedelta(seconds=core.schedule.observation_delay_seconds)
        <= now
        < closes - timedelta(seconds=core.schedule.entry_cutoff_seconds)
    ):
        return False
    with closing(core._db()) as db:
        state = core._load(db, session.day.isoformat())
    return state["armed_boot"] == core.boot_id and core._healthy(now)
