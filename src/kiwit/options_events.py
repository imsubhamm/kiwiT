"""Point-in-time event calendar adapter for options experiments."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import Path

from .intraday import IST


def event_context(now: datetime) -> dict:
    path = os.getenv("KIWIT_OPTIONS_EVENT_CALENDAR", "")
    if not path:
        return {"version": "event-calendar-v1", "coverage": "unconfigured", "risk": "unknown", "events": []}
    try:
        payload = json.loads(Path(path).read_text())
        as_of = datetime.fromisoformat(payload["as_of"])
        if as_of.tzinfo is None or not 0 <= (now - as_of).total_seconds() <= timedelta(days=30).total_seconds():
            raise ValueError("event calendar as_of is future, stale or lacks timezone")
        if not str(payload["owner"]).strip() or not str(payload["source_reference"]).strip():
            raise ValueError("event calendar owner and source_reference are required")
        events = []
        for item in payload.get("events", []):
            at = datetime.fromisoformat(item["at"])
            if at.tzinfo is None:
                raise ValueError("event timestamp lacks timezone")
            if abs((at - now).total_seconds()) <= timedelta(hours=2).total_seconds():
                events.append({"at": at.isoformat(), "name": str(item["name"])[:120],
                               "impact": item.get("impact", "unknown")})
        risk = "high" if any(event["impact"] == "high" for event in events) else "clear"
        return {"version": payload.get("version", "event-calendar-v1"), "coverage": "configured",
                "as_of": as_of.isoformat(), "owner": payload["owner"],
                "source_reference": payload["source_reference"], "local_day": str(now.astimezone(IST).date()),
                "risk": risk, "events": events}
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
        return {"version": "event-calendar-v1", "coverage": "invalid", "risk": "unknown", "events": [],
                "reason_code": "EVENT_CALENDAR_INVALID", "error_type": type(error).__name__}
