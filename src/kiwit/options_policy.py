"""Deterministic Bank Nifty entry gates and event-driven AI triggers."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from decimal import Decimal as D

from .options_events import CALENDAR_CONFIGURATION_MESSAGES, calendar_blocker_message, entry_calendar_blocker
from .options_risk import session_limit_reached

ENTRY_CAP = 10
COOLDOWN_SECONDS = 300
RECONSIDER_SECONDS = 120
MAX_EVENT_ATTEMPTS = 3

# Status precedence: hard risk limits, calendar configuration/event risk,
# position/cooldown constraints, then absence of an opportunity.
ENTRY_BLOCKER_MESSAGES = {
    "SESSION_PNL_LIMIT": "Session P&L limit reached; new entries disabled",
    "DAILY_ENTRY_CAP": "Daily entry cap reached; new entries disabled",
    **CALENDAR_CONFIGURATION_MESSAGES,
    "HIGH_IMPACT_EVENT_WINDOW": "High-impact event window active; waiting for event risk to clear",
    "ONE_POSITION_ACTIVE": "Existing position active; waiting for it to close",
    "POST_EXIT_COOLDOWN": "Post-exit cooldown active; waiting before the next entry",
    "NO_ELIGIBLE_PLAN": "No eligible entry plan; waiting for a supported setup",
}


def ordered_entry_reasons(reasons: list[str]) -> list[str]:
    priority = {code: index for index, code in enumerate(ENTRY_BLOCKER_MESSAGES)}
    return sorted(set(reasons), key=lambda code: (priority.get(code, len(priority)), code))


def entry_blocker_detail(gate: dict) -> str | None:
    """Render all current blockers in precedence order, independent of shadow plans."""
    reasons = ordered_entry_reasons(gate["reason_codes"])
    if not reasons:
        return None
    if len(reasons) > 1 and "NO_ELIGIBLE_PLAN" in reasons:
        reasons.remove("NO_ELIGIBLE_PLAN")
    calendar = gate.get("calendar_blocker") or {}
    return "; ".join(
        calendar_blocker_message(code, calendar.get("detail_code"))
        if code in CALENDAR_CONFIGURATION_MESSAGES
        else f"{ENTRY_BLOCKER_MESSAGES.get(code, 'Entry blocked')} [{code}]"
        for code in reasons
    )



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
    calendar = None
    if event_context is not None and not state.get("position"):
        calendar = entry_calendar_blocker(event_context)
        if calendar:
            reasons.append(calendar["reason_code"])
    return {
        "allowed": not reasons,
        "reason_codes": ordered_entry_reasons(reasons),
        "calendar_blocker": calendar,
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
                "pattern_at": plan.get("pattern_at"),
                **{field: str(D(str(plan[field])).normalize()) if plan.get(field) is not None else None
                   for field in ("underlying_trigger", "underlying_invalidation", "underlying_max_chase")},
                "symbol": plan.get("symbol"),
                "headroom_band": plan.get("decision_context", {}).get("price_headroom_band"),
                "chase_band": plan.get("decision_context", {}).get("chase_remaining_band"),
                "event_risk": plan.get("decision_context", {}).get("event_risk"),
            }
            for plan in plans
        ]
        material.sort(key=lambda item: json.dumps(item, sort_keys=True))
        kind = "ELIGIBLE_PLAN_SET_CHANGED"
    key = hashlib.sha256(json.dumps(material, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:32]
    return {"call": True, "kind": kind, "key": key, "material": material}


def reconsider_event(trigger: dict, attempts: list, now: datetime) -> dict:
    """Bound retries per event across polling, disappearance and worker restarts.

    Attempts are newest first (state, created_at, decision action) for this key.
    All rejections get a fresh review, never a replay of the rejected order;
    deterministic entry and execution authority remain responsible for safety.
    """
    reason = trigger["kind"]
    due = False
    if trigger["call"]:
        if not attempts:
            due, reason = True, "NEW_SETUP_OR_MATERIAL_CHANGE"
        else:
            status, created_at, action = attempts[0]
            retryable = status in {"failed", "interrupted", "rejected"} or (
                status == "applied" and action == "HOLD" and trigger["kind"] == "ELIGIBLE_PLAN_SET_CHANGED"
            )
            if len(attempts) >= MAX_EVENT_ATTEMPTS:
                reason = "EVENT_ATTEMPT_LIMIT"
            elif not retryable:
                reason = "UNCHANGED_EVENT"
            elif now < created_at + timedelta(seconds=RECONSIDER_SECONDS):
                reason = "RECONSIDERATION_COOLDOWN"
            else:
                due = True
                reason = "HOLD_RECONSIDERATION" if status == "applied" else "RETRY_" + status.upper()
    return {**trigger, "new": due, "reason": reason, "attempts": len(attempts),
            "max_attempts": MAX_EVENT_ATTEMPTS, "retry_after_seconds": RECONSIDER_SECONDS}
