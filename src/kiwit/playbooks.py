"""Versioned experimental paper playbooks. Eligibility is not evidence of an edge."""

import hashlib
import json
from datetime import datetime, timedelta
from decimal import Decimal as D

from .chart_analysis import VERSION as CHART_VERSION
from .options_risk import exit_levels, fill_price, quantity_for, sizing_diagnostics, trade_limits

VERSION = "banknifty-selector-v5-cost-aware"
PLAYBOOKS = (
    {
        "id": "opening_range_breakout_v5",
        "name": "Opening-range breakout",
        "pattern": "opening_range_breakout",
        "strategy": "momentum",
        "regime": "trend",
        "max_hold_minutes": 30,
        "live_exit": {"version": "orb-cost-aware-v1", "stop_pct": "4", "target_pct": "8",
                      "reward_r": "1.5", "max_hold_minutes": 30, "max_cost_share": "0.35"},
        "exit_experiments": {"risk_reward": ["1.5R", "2R", "2.5R"], "max_hold_minutes": [15, 30, 45]},
    },
    {
        "id": "breakout_retest_v5",
        "name": "Breakout / retest",
        "pattern": "breakout_retest",
        "strategy": "momentum",
        "regime": "trend",
        "max_hold_minutes": 45,
        "live_exit": {"version": "retest-cost-aware-v1", "stop_pct": "4", "target_pct": "8",
                      "reward_r": "2", "max_hold_minutes": 45, "max_cost_share": "0.35"},
        "exit_experiments": {"risk_reward": ["1.5R", "2R", "2.5R"], "max_hold_minutes": [15, 30, 45]},
    },
    {
        "id": "trend_pullback_v5",
        "name": "Trend pullback",
        "pattern": "ema_pullback",
        "strategy": "momentum",
        "regime": "trend",
        "max_hold_minutes": 30,
        "live_exit": {"version": "pullback-cost-aware-v1", "stop_pct": "3.5", "target_pct": "7",
                      "reward_r": "1.5", "max_hold_minutes": 30, "max_cost_share": "0.35"},
        "exit_experiments": {"risk_reward": ["1.5R", "2R", "2.5R"], "max_hold_minutes": [15, 30, 45]},
    },
    {
        "id": "range_reversal_v5",
        "name": "Range reversal",
        "pattern": "range_rejection",
        "strategy": "reversal",
        "regime": "range",
        "max_hold_minutes": 20,
        "live_exit": {"version": "range-reversal-cost-aware-v1", "stop_pct": "3", "target_pct": "6",
                      "reward_r": "1.25", "max_hold_minutes": 20, "max_cost_share": "0.35"},
        "exit_experiments": {"risk_reward": ["1R", "1.5R", "2R"], "max_hold_minutes": [10, 20, 30]},
    },
    {
        "id": "previous_day_breakout_v5",
        "name": "Previous-day breakout",
        "pattern": "previous_day_breakout",
        "strategy": "momentum",
        "regime": "trend",
        "max_hold_minutes": 45,
        "live_exit": {"version": "previous-day-cost-aware-v1", "stop_pct": "4", "target_pct": "8",
                      "reward_r": "2", "max_hold_minutes": 45, "max_cost_share": "0.35"},
        "exit_experiments": {"risk_reward": ["1.5R", "2R", "2.5R"], "max_hold_minutes": [15, 30, 45]},
    },
    {
        "id": "engulfing_reversal_v5",
        "name": "Engulfing reversal",
        "pattern": "engulfing",
        "strategy": "reversal",
        "regime": "any",
        "max_hold_minutes": 20,
        "live_exit": {"version": "engulfing-cost-aware-v1", "stop_pct": "3", "target_pct": "6",
                      "reward_r": "1.25", "max_hold_minutes": 20, "max_cost_share": "0.35"},
        "exit_experiments": {"risk_reward": ["1R", "1.5R", "2R"], "max_hold_minutes": [10, 20, 30]},
    },
    {
        "id": "hammer_reversal_v5",
        "name": "Hammer reversal",
        "pattern": "hammer",
        "strategy": "reversal",
        "regime": "range",
        "max_hold_minutes": 20,
        "live_exit": {"version": "hammer-cost-aware-v1", "stop_pct": "3", "target_pct": "6",
                      "reward_r": "1.5", "max_hold_minutes": 20, "max_cost_share": "0.35"},
        "exit_experiments": {"risk_reward": ["1R", "1.5R", "2R"], "max_hold_minutes": [10, 20, 30]},
    },
    {
        "id": "shooting_star_reversal_v5",
        "name": "Shooting-star reversal",
        "pattern": "shooting_star",
        "strategy": "reversal",
        "regime": "range",
        "max_hold_minutes": 20,
        "live_exit": {"version": "shooting-star-cost-aware-v1", "stop_pct": "3", "target_pct": "6",
                      "reward_r": "1.5", "max_hold_minutes": 20, "max_cost_share": "0.35"},
        "exit_experiments": {"risk_reward": ["1R", "1.5R", "2R"], "max_hold_minutes": [10, 20, 30]},
    },
)


def catalogue():
    return [dict(p, validation="unvalidated_paper_experiment") for p in PLAYBOOKS]


def playbook_by_id(playbook_id):
    return next((playbook for playbook in PLAYBOOKS if playbook["id"] == playbook_id), None)


def age_ok(stamp, now, seconds):
    return bool(stamp) and 0 <= (now - datetime.fromisoformat(stamp)).total_seconds() <= seconds


def route_reasons(analysis, pattern, playbook, now):
    reasons = []
    if (
        analysis.get("version") != CHART_VERSION
        or not analysis.get("ready")
        or not age_ok(analysis.get("at"), now, 180)
    ):
        reasons.append("Chart evidence incomplete or stale")
    if not age_ok(pattern.get("at"), now, 300):
        reasons.append("Setup expired or future-dated")
    direction = pattern.get("direction")
    if direction not in ("bullish", "bearish"):
        reasons.append("Invalid setup direction")
    frames = analysis.get("timeframes", {})
    required = None if playbook["regime"] == "any" else (
        "range" if playbook["regime"] == "range" else "uptrend" if direction == "bullish" else "downtrend"
    )
    if required and not any(frames.get(frame, {}).get("regime") == required for frame in ("5m", "15m")):
        reasons.append("Neither 5m nor 15m regime supports this playbook")
    if playbook["strategy"] == "reversal":
        supportive = {"range", "uptrend" if direction == "bullish" else "downtrend"}
        if frames.get("5m", {}).get("regime") not in supportive:
            reasons.append("5m regime does not confirm the reversal")
        if frames.get("15m", {}).get("regime") not in supportive:
            reasons.append("15m regime opposes the reversal")
    rsi_values = [D(str(frames[frame]["rsi14"])) for frame in ("5m", "15m")
                  if frames.get(frame, {}).get("rsi14") is not None]
    if direction == "bullish" and any(rsi >= 75 for rsi in rsi_values):
        reasons.append("Bullish entry blocked by RSI exhaustion")
    if direction == "bearish" and any(rsi <= 25 for rsi in rsi_values):
        reasons.append("Bearish entry blocked by RSI exhaustion")
    week = analysis.get("previous_calendar_week") or {}
    if week.get("coverage", {}).get("status") != "complete":
        reasons.append("Previous-week coverage incomplete")
    return reasons


def quote_ok(quote, now):
    if not age_ok(quote.get("stamp"), now, 90):
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
            events = snapshot.get("event_context") or {}
            if events.get("coverage") != "configured":
                rejected.append("Verified event calendar unavailable; entry fails closed")
            elif events.get("risk") != "clear":
                rejected.append("Verified high-impact event window blocks entry")
            if rejected:
                reasons.extend(rejected)
                continue
            kind = "CE" if pattern["direction"] == "bullish" else "PE"
            atr_value = analysis["timeframes"]["5m"].get("atr14")
            atr = D(str(atr_value)) if atr_value is not None else D(0)
            if not atr.is_finite() or atr <= 0:
                reasons.append("ATR unavailable for entry price bound")
                continue
            trigger = D(str(pattern["observed_close"]))
            invalidation = D(str(pattern["invalidation"]))
            chase = trigger + (D("0.25") * atr if kind == "CE" else -D("0.25") * atr)
            spot = D(snapshot["spot"])
            if not spot.is_finite() or not (
                invalidation < trigger <= spot <= chase if kind == "CE" else chase <= spot <= trigger < invalidation
            ):
                reasons.append("Current underlying outside trigger/chase bounds")
                continue
            contracts = [
                c
                for c in snapshot["candidates"]
                if c.get("selection_eligible", True)
                and c["kind"] == kind and c["expiry"] > state["day"] and quote_ok(c["quote"], now)
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
                    size = sizing_diagnostics(state, contract, capped)
                    reasons.append(f"{contract['symbol']}: whole lot cannot fit cash/risk/depth; "
                                   f"estimated initial capital >= INR {size['minimum_initial_capital_estimate']}")
                    continue
                fill = fill_price(contract["quote"], contract, True)
                try:
                    exits = exit_levels(state, playbook["live_exit"], fill, qty, contract)
                except ValueError as error:
                    reasons.append(f"{contract['symbol']}: {error}")
                    continue
                expires = min(
                    datetime.fromisoformat(pattern["at"]) + timedelta(seconds=300),
                    datetime.fromisoformat(analysis["at"]) + timedelta(seconds=180),
                    now + timedelta(seconds=120),
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
                    "underlying_trigger": str(trigger),
                    "underlying_invalidation": str(invalidation),
                    "underlying_max_chase": str(chase),
                    "max_fill": str(fill_price(capped, contract, True)),
                    "planned_fill": str(fill),
                    "quantity": qty,
                    "planned_stop": str(exits["stop"]),
                    "planned_target": str(exits["target"]),
                    "loss_pct": str(trade_limits(state)[0]),
                    "profit_pct": str(trade_limits(state)[1]),
                    "max_hold_minutes": exits["max_hold_minutes"],
                    "exit_policy": "playbook_cost_aware_v5",
                    "live_exit": playbook["live_exit"],
                    "cost_aware_exit": {key: str(value) if isinstance(value, D) else value
                                        for key, value in exits.items()},
                    "exit_experiments": playbook["exit_experiments"],
                }
                spread = (D(contract["quote"]["ask"]) - D(contract["quote"]["bid"])) / D(contract["quote"]["ask"])
                quote_age = (now - datetime.fromisoformat(contract["quote"]["stamp"])).total_seconds()
                headroom = (D(chosen["max_fill"]) - fill) / fill
                chase_remaining = (chase - spot) / atr if kind == "CE" else (spot - chase) / atr
                invalidation_distance = abs(spot - invalidation) / atr
                chosen["decision_context"] = {
                    "plan_valid_now": True,
                    "quote_age_seconds": quote_age,
                    "spread_pct": str(spread * 100),
                    "price_headroom_pct": str(headroom * 100),
                    "price_headroom_band": "tight" if headroom < D(".0025") else "available",
                    "invalidation_distance_atr": str(invalidation_distance),
                    "chase_allowance_atr": "0.25",
                    "chase_remaining_atr": str(chase_remaining),
                    "chase_remaining_band": "tight" if chase_remaining < D(".05") else "available",
                    "event_risk": snapshot.get("event_context", {}).get("risk", "unknown"),
                    "feature_coverage": snapshot.get("option_feature_coverage", {}),
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
    if (D(plan["loss_pct"]), D(plan["profit_pct"])) != (trade_limits(state)[0], trade_limits(state)[1]):
        raise ValueError("Entry plan risk limits changed")
    playbook = playbook_by_id(plan["playbook_id"])
    if not playbook or plan.get("live_exit") != playbook["live_exit"]:
        raise ValueError("Entry plan exit profile changed")
    events = snapshot.get("event_context") or {}
    if events.get("coverage") != "configured" or events.get("risk") != "clear":
        raise ValueError("Verified clear event calendar required for entry")
    if not age_ok(underlying.get("at"), now, 180) or datetime.fromisoformat(underlying["at"]) < datetime.fromisoformat(
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
    exits = exit_levels(state, playbook["live_exit"], fill_price(quote, contract, True), plan["quantity"], contract)
    if exits["net_reward_at_target"] <= 0 or exits["net_reward_r"] < D(str(playbook["live_exit"]["reward_r"])):
        raise ValueError("Cost-aware target does not provide required net reward")
    return plan


def underlying_exit(position, sample, now):
    plan = position.get("entry_plan")
    if not plan or not sample or not age_ok(sample.get("at"), now, 180):
        return False
    if datetime.fromisoformat(sample["at"]) < datetime.fromisoformat(position["entered_at"]):
        return False
    price, level = D(str(sample["spot"])), D(str(plan["underlying_invalidation"]))
    return price.is_finite() and (price <= level if plan["kind"] == "CE" else price >= level)
