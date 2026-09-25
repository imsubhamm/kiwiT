"""Point-in-time event calendar adapter for options experiments."""

from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

from .intraday import IST

CALENDAR_VERSION = "event-calendar-v2"
MAX_CALENDAR_AGE = timedelta(days=7)
ALLOWED_IMPACTS = frozenset({"low", "medium", "high"})


class EventCalendarValidationError(ValueError):
    """A safe, machine-readable calendar validation failure."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _timestamp(value: object, code: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError) as error:
        raise EventCalendarValidationError(code) from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise EventCalendarValidationError(code)
    return parsed


def _day(value: object, code: str) -> date:
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError) as error:
        raise EventCalendarValidationError(code) from error


def _source(value: object, code: str) -> str:
    source = str(value or "").strip()
    parsed = urlparse(source)
    if parsed.scheme != "https" or not parsed.netloc:
        raise EventCalendarValidationError(code)
    return source


def _load_calendar(path: Path, now: datetime) -> dict:
    try:
        payload = json.loads(path.read_text())
    except OSError as error:
        raise EventCalendarValidationError("CALENDAR_NOT_READABLE") from error
    except json.JSONDecodeError as error:
        raise EventCalendarValidationError("CALENDAR_JSON_INVALID") from error
    if not isinstance(payload, dict):
        raise EventCalendarValidationError("CALENDAR_ROOT_INVALID")
    if payload.get("version") != CALENDAR_VERSION:
        raise EventCalendarValidationError("CALENDAR_VERSION_UNSUPPORTED")

    as_of = _timestamp(payload.get("as_of"), "CALENDAR_AS_OF_INVALID")
    age = now.astimezone(as_of.tzinfo) - as_of
    if age < timedelta(0):
        raise EventCalendarValidationError("CALENDAR_AS_OF_FUTURE")
    if age > MAX_CALENDAR_AGE:
        raise EventCalendarValidationError("CALENDAR_STALE")

    owner = str(payload.get("owner") or "").strip()
    if not owner:
        raise EventCalendarValidationError("CALENDAR_OWNER_MISSING")
    source_reference = _source(payload.get("source_reference"), "CALENDAR_SOURCE_INVALID")

    coverage_start = _day(payload.get("coverage_start"), "CALENDAR_COVERAGE_INVALID")
    coverage_end = _day(payload.get("coverage_end"), "CALENDAR_COVERAGE_INVALID")
    if coverage_start > coverage_end:
        raise EventCalendarValidationError("CALENDAR_COVERAGE_INVALID")
    local_day = now.astimezone(IST).date()
    if not coverage_start <= local_day <= coverage_end:
        raise EventCalendarValidationError("CALENDAR_DAY_NOT_COVERED")

    raw_events = payload.get("events")
    if not isinstance(raw_events, list):
        raise EventCalendarValidationError("CALENDAR_EVENTS_INVALID")
    events = []
    for item in raw_events:
        if not isinstance(item, dict):
            raise EventCalendarValidationError("CALENDAR_EVENT_INVALID")
        at = _timestamp(item.get("at"), "CALENDAR_EVENT_TIME_INVALID")
        if not coverage_start <= at.astimezone(IST).date() <= coverage_end:
            raise EventCalendarValidationError("CALENDAR_EVENT_OUTSIDE_COVERAGE")
        name = str(item.get("name") or "").strip()[:120]
        if not name:
            raise EventCalendarValidationError("CALENDAR_EVENT_NAME_MISSING")
        impact = str(item.get("impact") or "").strip().lower()
        if impact not in ALLOWED_IMPACTS:
            raise EventCalendarValidationError("CALENDAR_EVENT_IMPACT_INVALID")
        events.append({
            "at": at.isoformat(),
            "name": name,
            "impact": impact,
            "source_reference": _source(
                item.get("source_reference"), "CALENDAR_EVENT_SOURCE_INVALID"
            ),
        })

    nearby = [
        event for event in events
        if abs((_timestamp(event["at"], "CALENDAR_EVENT_TIME_INVALID") - now).total_seconds())
        <= timedelta(hours=2).total_seconds()
    ]
    risk = "high" if any(event["impact"] == "high" for event in nearby) else "clear"
    return {
        "version": CALENDAR_VERSION,
        "coverage": "configured",
        "as_of": as_of.isoformat(),
        "coverage_start": coverage_start.isoformat(),
        "coverage_end": coverage_end.isoformat(),
        "owner": owner,
        "source_reference": source_reference,
        "local_day": str(local_day),
        "risk": risk,
        "events": nearby,
    }


def event_context(now: datetime, path: str | Path | None = None) -> dict:
    """Return validated nearby event risk, failing closed with a safe reason code."""
    configured_path = str(path or os.getenv("KIWIT_OPTIONS_EVENT_CALENDAR", "")).strip()
    if not configured_path:
        return {
            "version": CALENDAR_VERSION,
            "coverage": "unconfigured",
            "risk": "unknown",
            "events": [],
            "reason_code": "CALENDAR_PATH_UNCONFIGURED",
        }
    try:
        return _load_calendar(Path(configured_path), now)
    except EventCalendarValidationError as error:
        return {
            "version": CALENDAR_VERSION,
            "coverage": "invalid",
            "risk": "unknown",
            "events": [],
            "reason_code": error.code,
        }
