"""Deterministic Bank Nifty entry gates and event-driven AI triggers."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from decimal import Decimal as D

from .options_risk import session_limit_reached

ENTRY_CAP = 10
COOLDOWN_SECONDS = 300


def entry_gate(state: dict, now: datetime, plans: list[dict], event_context: dict | None = None) -> dict:
    """Return machine-readable entry authority before any paid model call."""
    reasons = []
    if state.get("position"):
        reasons.append("ONE_POSITION_ACTIVE")
    if int(state.get("entries", 0)) >= ENTRY_CAP:
        reasons.append("DAILY_ENTRY_CAP")
    if session_limit_reached(state, D(state.get("realized_pnl", "0"))):
        reasons.append("SESSION_PNL_LIMIT")
    last_exit = state.get("last_exit")
    remaining = 0
    if last_exit:
        remaining = max(0, COOLDOWN_SECONDS - int((now - datetime.fromisoformat(last_exit)).total_seconds()))
        if remaining:
            reasons.append("POST_EXIT_COOLDOWN")
    if not plans and not state.get("position"):
        reasons.append("NO_ELIGIBLE_PLAN")
    events = event_context or {}
    if event_context is not None and not state.get("position"):
        if events.get("coverage") != "configured":
            reasons.append("EVENT_CALENDAR_UNAVAILABLE")
        elif events.get("risk") != "clear":
            reasons.append("HIGH_IMPACT_EVENT_WINDOW")
    return {
        "allowed": not reasons,
        "reason_codes": reasons,
        "cooldown_remaining_seconds": remaining,
        "entry_cap": ENTRY_CAP,
        "entries_remaining": max(0, ENTRY_CAP - int(state.get("entries", 0))),
    }


def decision_event(snapshot: dict) -> dict:
    """Describe material opportunity changes; timestamps alone never trigger a call."""
    position = snapshot.get("position")
    if position:
        mark = D(str(position.get("mark", position.get("entry", "NaN"))))
        stop = D(str(position.get("stop", "NaN")))
        target = D(str(position.get("target", "NaN")))
        if not all(value.is_finite() for value in (mark, stop, target)):
            return {"call": False, "kind": "POSITION_NO_FRESH_MARK", "key": None}
        band = "near_stop" if mark <= stop * D("1.02") else "near_target" if mark >= target * D(".98") else "normal"
        if band == "normal":
            return {"call": False, "kind": "POSITION_DETERMINISTIC_MONITOR", "key": None}
        material = {"position_id": position.get("id"), "band": band}
        kind = "POSITION_RISK_BAND"
    else:
        plans = snapshot.get("strategy_selection", {}).get("plans", [])
        if not plans:
            return {"call": False, "kind": "NO_ELIGIBLE_PLAN", "key": None}
        material = [
            {
                "playbook": plan.get("playbook_id"),
                "pattern": plan.get("pattern_id"),
                "symbol": plan.get("symbol"),
                "headroom_band": plan.get("decision_context", {}).get("price_headroom_band"),
                "chase_band": plan.get("decision_context", {}).get("chase_remaining_band"),
                "event_risk": plan.get("decision_context", {}).get("event_risk"),
            }
            for plan in plans
        ]
        kind = "ELIGIBLE_PLAN_SET_CHANGED"
    key = hashlib.sha256(json.dumps(material, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:32]
    return {"call": True, "kind": kind, "key": key, "material": material}
