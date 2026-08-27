"""Versioned experimental paper playbooks. Eligibility is not evidence of an edge."""

import hashlib
import json
from datetime import datetime, timedelta
from decimal import Decimal as D

from .chart_analysis import VERSION as CHART_VERSION
from .options_risk import fill_price, quantity_for

VERSION = "banknifty-selector-v1"
PLAYBOOKS = (
    {
        "id": "opening_range_breakout_v1",
        "name": "Opening-range breakout",
        "pattern": "opening_range_breakout",
        "strategy": "momentum",
        "regime": "trend",
        "max_hold_minutes": 45,
    },
    {
        "id": "breakout_retest_v1",
        "name": "Breakout / retest",
        "pattern": "breakout_retest",
        "strategy": "momentum",
        "regime": "trend",
        "max_hold_minutes": 45,
    },
    {
        "id": "trend_pullback_v1",
        "name": "Trend pullback",
        "pattern": "ema_pullback",
        "strategy": "momentum",
        "regime": "trend",
        "max_hold_minutes": 45,
    },
    {
        "id": "range_reversal_v1",
        "name": "Range reversal",
        "pattern": "range_rejection",
        "strategy": "reversal",
        "regime": "range",
        "max_hold_minutes": 30,
    },
)


def catalogue():
    return [dict(p, validation="unvalidated_paper_experiment") for p in PLAYBOOKS]


def age_ok(stamp, now, seconds):
    return bool(stamp) and 0 <= (now - datetime.fromisoformat(stamp)).total_seconds() <= seconds


def route_reasons(analysis, pattern, playbook, now):
    reasons = []
    if (
        analysis.get("version") != CHART_VERSION
        or not analysis.get("ready")
        or not age_ok(analysis.get("at"), now, 120)
    ):
        reasons.append("Chart evidence incomplete or stale")
    if not age_ok(pattern.get("at"), now, 300):
        reasons.append("Setup expired or future-dated")
    direction = pattern.get("direction")
    if direction not in ("bullish", "bearish"):
        reasons.append("Invalid setup direction")
    frames = analysis.get("timeframes", {})
    required = "range" if playbook["regime"] == "range" else "uptrend" if direction == "bullish" else "downtrend"
    if any(frames.get(frame, {}).get("regime") != required for frame in ("5m", "15m")):
        reasons.append("5m/15m regimes do not support this playbook")
    week = analysis.get("previous_calendar_week") or {}
    if week.get("coverage", {}).get("status") != "complete":
        reasons.append("Previous-week coverage incomplete")
    elif (week.get("trend"), direction) in {("upward_bias", "bearish"), ("downward_bias", "bullish")}:
        reasons.append("Direction conflicts with previous-week bias")
    return reasons


def quote_ok(quote, now):
    if not age_ok(quote.get("stamp"), now, 60):
        return False
    bid, ask = D(quote["bid"]), D(quote["ask"])
    return bid.is_finite() and ask.is_finite() and 0 < bid <= ask and (ask - bid) / ask <= D(".02")


def fingerprint(plan):
    return hashlib.sha256(json.dumps(plan, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:32]


def select_plans(snapshot, state, now):
    """At most one deterministic contract/plan per playbook; no unbounded prompt growth."""
    analysis = snapshot.get("chart_analysis") or {}
    selection = {
        "version": VERSION,
        "at": now.isoformat(),
        "validation": "unvalidated_paper_experiment",
        "evaluations": [],
        "plans": [],
    }
    for playbook in PLAYBOOKS:
        patterns = sorted(
            (p for p in analysis.get("patterns", []) if p.get("name") == playbook["pattern"]),
            key=lambda p: (p["at"], p["id"]),
            reverse=True,
        )
        reasons, chosen = [], None
        for pattern in patterns:
            rejected = route_reasons(analysis, pattern, playbook, now)
            if rejected:
                reasons.extend(rejected)
                continue
            kind = "CE" if pattern["direction"] == "bullish" else "PE"
            atr = analysis["timeframes"]["5m"].get("atr14")
            if not atr or not D(str(atr)).is_finite() or atr <= 0:
                reasons.append("ATR unavailable for entry price bound")
                continue
            trigger = pattern["observed_close"]
            invalidation = pattern["invalidation"]
            chase = trigger + (0.5 * atr if kind == "CE" else -0.5 * atr)
            spot = D(snapshot["spot"])
            if not spot.is_finite() or not (
                invalidation < trigger <= spot <= chase if kind == "CE" else chase <= spot <= trigger < invalidation
            ):
                reasons.append("Current underlying outside trigger/chase bounds")
                continue
            contracts = [
                c
                for c in snapshot["candidates"]
                if c["kind"] == kind and c["expiry"] > state["day"] and quote_ok(c["quote"], now)
            ]
            contracts.sort(
                key=lambda c: (
                    (D(c["quote"]["ask"]) - D(c["quote"]["bid"])) / D(c["quote"]["ask"]),
                    abs(D(c["strike"]) - D(snapshot["spot"])),
                    c["symbol"],
                )
            )
            for contract in contracts:
                capped = dict(contract["quote"], ask=str(D(contract["quote"]["ask"]) * D("1.005")))
                qty = quantity_for(state, contract, capped)
                if not qty:
                    continue
                fill = fill_price(contract["quote"], contract, True)
                expires = min(
                    datetime.fromisoformat(pattern["at"]) + timedelta(seconds=300),
                    datetime.fromisoformat(analysis["at"]) + timedelta(seconds=120),
                    now + timedelta(seconds=90),
                )
                if expires <= now:
                    continue
                chosen = {
                    "selector_version": VERSION,
                    "playbook_id": playbook["id"],
                    "strategy": playbook["strategy"],
                    "symbol": contract["symbol"],
                    "kind": kind,
                    "pattern_id": pattern["id"],
                    "pattern_at": pattern["at"],
                    "created_at": now.isoformat(),
                    "expires_at": expires.isoformat(),
                    "underlying_trigger": trigger,
                    "underlying_invalidation": invalidation,
                    "underlying_max_chase": chase,
                    "max_fill": str(fill_price(capped, contract, True)),
                    "planned_fill": str(fill),
                    "quantity": qty,
                    "planned_stop": str(fill * (1 - D(state["loss_pct"]) / 100)),
                    "planned_target": str(fill * (1 + D(state["profit_pct"]) / 100)),
                    "loss_pct": state["loss_pct"],
                    "profit_pct": state["profit_pct"],
                    "max_hold_minutes": playbook["max_hold_minutes"],
                }
                chosen["id"] = fingerprint(chosen)
                break
            if chosen:
                break
            reasons.append("No affordable liquid contract or valid unexpired trigger")
        if chosen:
            selection["plans"].append(chosen)
        selection["evaluations"].append(
            {
                "playbook_id": playbook["id"],
                "eligible": bool(chosen),
                "reasons": ["Fresh setup and aligned regimes; awaiting AI selection"]
                if chosen
                else sorted(set(reasons)) or ["No fresh matching chart setup"],
            }
        )
    return selection


def validate_plan(decision, snapshot, state, quote, underlying, now):
    selection = snapshot.get("strategy_selection") or {}
    plan = next((p for p in selection.get("plans", []) if p["id"] == decision.get("plan_id")), None)
    if not plan or selection.get("version") != VERSION:
        raise ValueError("AI selected an unknown entry plan")
    if plan["selector_version"] != VERSION or fingerprint({k: v for k, v in plan.items() if k != "id"}) != plan["id"]:
        raise ValueError("Entry plan version or integrity mismatch")
    if plan["symbol"] != decision["symbol"] or plan["strategy"] != decision["strategy"]:
        raise ValueError("AI decision does not match its entry plan")
    if not datetime.fromisoformat(plan["created_at"]) <= now < datetime.fromisoformat(plan["expires_at"]):
        raise ValueError("Entry plan expired or future-dated")
    if (D(plan["loss_pct"]), D(plan["profit_pct"])) != (D(state["loss_pct"]), D(state["profit_pct"])):
        raise ValueError("Entry plan risk limits changed")
    if not age_ok(underlying.get("at"), now, 120) or datetime.fromisoformat(underlying["at"]) < datetime.fromisoformat(
        snapshot["spot_at"]
    ):
        raise ValueError("Entry recheck underlying is stale or older than decision")
    spot = D(str(underlying["spot"]))
    trigger, invalidation, chase = (
        D(str(plan[k])) for k in ("underlying_trigger", "underlying_invalidation", "underlying_max_chase")
    )
    valid = spot.is_finite() and (
        invalidation < trigger <= spot <= chase if plan["kind"] == "CE" else chase <= spot <= trigger < invalidation
    )
    if not valid:
        raise ValueError("Underlying trigger invalidated or entry chase limit exceeded")
    contract = next(c for c in snapshot["candidates"] if c["symbol"] == plan["symbol"])
    if not quote_ok(quote, now) or fill_price(quote, contract, True) > D(plan["max_fill"]):
        raise ValueError("Option spread, freshness or entry price limit failed")
    if plan["quantity"] <= 0 or plan["quantity"] > quantity_for(state, contract, quote):
        raise ValueError("Planned quantity no longer fits cash, risk or liquidity")
    return plan


def underlying_exit(position, sample, now):
    plan = position.get("entry_plan")
    if not plan or not sample or not age_ok(sample.get("at"), now, 120):
        return False
    if datetime.fromisoformat(sample["at"]) < datetime.fromisoformat(position["entered_at"]):
        return False
    price, level = D(str(sample["spot"])), D(str(plan["underlying_invalidation"]))
    return price.is_finite() and (price <= level if plan["kind"] == "CE" else price >= level)
