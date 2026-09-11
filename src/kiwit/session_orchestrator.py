"""Durable paper-session lifecycle with explicit exchange calendar and manual RUN."""

import json
import sqlite3
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import timedelta
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from .marketdata.canonical import IST, utc
from .ml.datasets import _hash, _json

REQUIRED = ("database", "configuration", "groww_auth", "history", "features", "models", "audit")


class SessionBackend(Protocol):
    """Paper-only adapter. Side effects MUST deduplicate the supplied durable event keys."""

    def health(self, now): ...  # {as_of: aware datetime, checks: {required_name: bool}}
    def warm(self, session_id): ...  # Return True only after required state is ready.
    def observe(self, event_id, now, *, entries_allowed): ...
    def close(self, session_id, now, *, policy): ...  # True means no unresolved positions/orders.
    def report(self, session_id): ...  # JSON-safe EOD state, no credentials.


@dataclass(frozen=True)
class SessionSchedule:
    timezone: str = "Asia/Kolkata"
    premarket_seconds: int = 900
    observation_delay_seconds: int = 0
    entry_cutoff_seconds: int = 900
    close_policy: str = "FLATTEN_ON_FRESH_QUOTE"
    health_max_age_seconds: int = 30

    def __post_init__(self):
        if self.timezone != "Asia/Kolkata":
            raise ValueError("Calendar timezone must explicitly be Asia/Kolkata")
        for name in (
            "premarket_seconds",
            "observation_delay_seconds",
            "entry_cutoff_seconds",
            "health_max_age_seconds",
        ):
            if type(getattr(self, name)) is not int or getattr(self, name) < 0:
                raise ValueError("Schedule offsets must be nonnegative integer seconds")
        if self.health_max_age_seconds == 0 or self.entry_cutoff_seconds == 0:
            raise ValueError("Positive health age and entry cutoff required")
        if self.close_policy not in {"FLATTEN_ON_FRESH_QUOTE", "HOLD_FOR_RECONCILIATION"}:
            raise ValueError("Unsupported closing policy")


class SessionOrchestrator:
    def __init__(self, path, *, calendar, backend: SessionBackend, schedule=None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.calendar, self.backend = calendar, backend
        self.schedule = schedule or SessionSchedule()
        self.boot_id = str(uuid4())
        self.binding = _hash(
            {
                "schedule": asdict(self.schedule),
                "calendar": [
                    {"day": s.day.isoformat(), "open": s.opens.isoformat(), "close": s.closes.isoformat()}
                    for s in calendar.sessions
                ],
                "calendar_version": calendar.version,
            }
        )
        for session in calendar.sessions:
            opens, closes = session.bounds()
            if opens + timedelta(seconds=self.schedule.observation_delay_seconds) >= closes - timedelta(
                seconds=self.schedule.entry_cutoff_seconds
            ):
                raise ValueError("Observation window ends after entry cutoff")
        with closing(self._db()) as db, db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS market_sessions (
                    day TEXT PRIMARY KEY, binding TEXT NOT NULL, body TEXT NOT NULL);
            """)

    def _db(self):
        # Each call opens a short-lived connection; tick holds the write lock through adapter calls.
        return sqlite3.connect(self.path, timeout=30)

    def _load(self, db, day):
        row = db.execute("SELECT binding,body FROM market_sessions WHERE day=?", (day,)).fetchone()
        if row and row[0] != self.binding:
            raise ValueError("Session config/calendar changed; reconcile before migration")
        return (
            json.loads(row[1])
            if row
            else {
                "day": day,
                "session_id": _hash({"day": day, "binding": self.binding}),
                "state": "WAITING",
                "armed_boot": None,
                "operator": None,
                "warmed_boot": None,
                "last_tick": None,
                "events": [],
                "report": None,
            }
        )

    def _save(self, db, state):
        db.execute(
            "INSERT INTO market_sessions VALUES (?,?,?) ON CONFLICT(day) DO UPDATE SET body=excluded.body",
            (state["day"], self.binding, _json(state)),
        )

    def _healthy(self, now):
        health = self.backend.health(now)
        age = (now - utc(health["as_of"])).total_seconds()
        return 0 <= age <= self.schedule.health_max_age_seconds and all(
            health["checks"].get(k) is True for k in REQUIRED
        )

    def control(self, *, now, action, operator):
        now = utc(now)
        if action not in {"RUN", "STOP"} or not operator.strip():
            raise ValueError("Explicit RUN/STOP and operator identity required")
        session = self.calendar.session(now.astimezone(IST).date())
        if session is None:
            raise ValueError("No configured market session")
        opens, closes = session.bounds()
        if action == "RUN" and not opens - timedelta(
            seconds=self.schedule.premarket_seconds
        ) <= now < closes - timedelta(seconds=self.schedule.entry_cutoff_seconds):
            raise ValueError("RUN outside configured activation window")
        with closing(self._db()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            state = self._load(db, session.day.isoformat())
            if state["state"] == "COMPLETE":
                raise ValueError("Session already complete")
            state["armed_boot"] = self.boot_id if action == "RUN" else None
            state["operator"] = operator
            state["events"].append({"at": now.isoformat(), "action": action, "operator": operator})
            self._save(db, state)
        return state

    def _close(self, state, now):
        state["armed_boot"] = None
        state["state"] = "CLOSING"
        if self.backend.close(state["session_id"], now, policy=self.schedule.close_policy) is True:
            report = self.backend.report(state["session_id"])
            _json(report)
            state["report"] = report
            state["state"] = "COMPLETE"

    def tick(self, now):
        now = utc(now)
        day = now.astimezone(IST).date().isoformat()
        db = self._db()
        try:
            with db:
                db.execute("BEGIN IMMEDIATE")
                # Reconcile unfinished prior days before starting another session, including holidays.
                older = db.execute("SELECT day,body FROM market_sessions WHERE day<? ORDER BY day", (day,)).fetchall()
                for old_day, body in older:
                    old = json.loads(body)
                    if old["state"] != "COMPLETE":
                        state = self._load(db, old_day)
                        self._advance_close(state, now)
                        self._save(db, state)
                        if state["state"] != "COMPLETE":
                            return state
                session = self.calendar.session(now.astimezone(IST).date())
                if session is None:
                    return {"day": day, "state": "CLOSED_CALENDAR", "entries_allowed": False}
                state = self._load(db, day)
                if state["last_tick"] is not None and now.isoformat() <= state["last_tick"]:
                    raise ValueError("Tick timestamps must strictly increase")
                if state["warmed_boot"] not in {None, self.boot_id}:
                    state["armed_boot"] = None
                state["last_tick"] = now.isoformat()
                state["entries_allowed"] = False
                opens, closes = session.bounds()
                try:
                    if state["state"] == "COMPLETE":
                        pass
                    elif now >= closes:
                        self._close(state, now)
                    elif now < opens - timedelta(seconds=self.schedule.premarket_seconds):
                        state["state"] = "WAITING"
                    elif not self._healthy(now):
                        state["state"] = "BLOCKED_DEPENDENCIES"
                        state["armed_boot"] = None
                        # Keep exit/reconciliation observation alive while entries are blocked.
                        if now >= opens:
                            self.backend.observe(
                                state["session_id"] + ":" + now.isoformat(), now, entries_allowed=False
                            )
                    else:
                        if state["warmed_boot"] != self.boot_id:
                            if self.backend.warm(state["session_id"]) is not True:
                                raise RuntimeError("Warmup incomplete")
                            state["warmed_boot"] = self.boot_id
                        if now < opens + timedelta(seconds=self.schedule.observation_delay_seconds):
                            state["state"] = "READY"
                        else:
                            allowed = state["armed_boot"] == self.boot_id and now < closes - timedelta(
                                seconds=self.schedule.entry_cutoff_seconds
                            )
                            state["state"] = "OBSERVING" if allowed else "OBSERVING_NO_ENTRIES"
                            self.backend.observe(
                                state["session_id"] + ":" + now.isoformat(), now, entries_allowed=allowed
                            )
                            state["entries_allowed"] = allowed
                except Exception as error:  # noqa: BLE001 - persist fail-closed adapter failures  # Adapter failure must persist a fail-closed state.
                    state.update(state="ERROR", armed_boot=None, entries_allowed=False, error_type=type(error).__name__)
                self._save(db, state)
                return state
        finally:
            db.close()

    def _advance_close(self, state, now):
        state["entries_allowed"] = False
        try:
            self._close(state, now)
        except Exception as error:  # noqa: BLE001 - persist fail-closed adapter failures
            state.update(state="ERROR", armed_boot=None, error_type=type(error).__name__)
