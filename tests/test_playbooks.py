from copy import deepcopy
from datetime import datetime, timedelta

import pytest

from kiwit.chart_analysis import VERSION as CHART_VERSION
from kiwit.intraday import IST
from kiwit.options_ai import request_body
from kiwit.options_risk import quantity_for
from kiwit.playbooks import PLAYBOOKS, select_plans, underlying_exit, validate_plan

NOW = datetime(2026, 8, 26, 10, 0, tzinfo=IST)


def fixtures(kind="CE", playbook=PLAYBOOKS[0]):
    direction = "bullish" if kind == "CE" else "bearish"
    regime = "range" if playbook["regime"] == "range" else "uptrend" if kind == "CE" else "downtrend"
    quote = {"stamp": NOW.isoformat(), "bid": "100", "ask": "101", "bid_size": 600, "ask_size": 600}
    contract = {
        "symbol": "BANKNIFTY26SEP55000" + kind,
        "kind": kind,
        "expiry": "2026-09-29",
        "strike": "55000",
        "lot": 30,
        "freeze": 601,
        "tick": ".05",
        "quote": quote,
    }
    state = {
        "day": "2026-08-26",
        "amount": "100000",
        "cash": "100000",
        "loss_pct": "5",
        "profit_pct": "10",
        "realized_pnl": "0",
    }
    pattern = {
        "id": playbook["pattern"] + "_" + direction,
        "name": playbook["pattern"],
        "direction": direction,
        "strategy": playbook["strategy"],
        "at": NOW.isoformat(),
        "observed_close": 55000,
        "invalidation": 54950 if kind == "CE" else 55050,
    }
    analysis = {
        "version": CHART_VERSION,
        "ready": True,
        "at": NOW.isoformat(),
        "patterns": [pattern],
        "timeframes": {"5m": {"regime": regime, "atr14": 100}, "15m": {"regime": regime}},
        "previous_calendar_week": {"coverage": {"status": "complete"}, "trend": "mixed_or_range"},
    }
    snapshot = {"spot": "55000", "spot_at": NOW.isoformat(), "candidates": [contract], "chart_analysis": analysis}
    snapshot["strategy_selection"] = select_plans(snapshot, state, NOW)
    return snapshot, state, quote


def decision(snapshot):
    plan = snapshot["strategy_selection"]["plans"][0]
    return {
        "action": "BUY",
        "symbol": plan["symbol"],
        "strategy": plan["strategy"],
        "plan_id": plan["id"],
        "summary": "Fixture",
    }


@pytest.mark.parametrize("kind", ["CE", "PE"])
@pytest.mark.parametrize("playbook", PLAYBOOKS)
def test_all_playbooks_route_with_explicit_immutable_plan(kind, playbook):
    snapshot, state, quote = fixtures(kind, playbook)
    selection = snapshot["strategy_selection"]
    assert len(selection["plans"]) == 1
    plan = selection["plans"][0]
    assert plan["playbook_id"] == playbook["id"]
    assert plan["quantity"] > 0 and plan["quantity"] % 30 == 0
    assert select_plans(snapshot, state, NOW) == selection
    assert (
        validate_plan(decision(snapshot), snapshot, state, quote, {"at": NOW.isoformat(), "spot": "55000"}, NOW) == plan
    )


@pytest.mark.parametrize(
    "change,reason",
    [
        ("week_conflict", "previous-week bias"),
        ("frame_conflict", "regimes"),
        ("week_missing", "coverage"),
        ("stale", "stale"),
        ("expired", "expired"),
        ("future", "future"),
        ("unready", "incomplete"),
    ],
)
def test_router_rejects_unsupported_context(change, reason):
    snapshot, state, _ = fixtures()
    a = snapshot["chart_analysis"]
    if change == "week_conflict":
        a["previous_calendar_week"]["trend"] = "downward_bias"
    if change == "frame_conflict":
        a["timeframes"]["15m"]["regime"] = "downtrend"
    if change == "week_missing":
        a["previous_calendar_week"] = {}
    if change == "stale":
        a["at"] = (NOW - timedelta(seconds=121)).isoformat()
    if change == "expired":
        a["patterns"][0]["at"] = (NOW - timedelta(seconds=301)).isoformat()
    if change == "future":
        a["patterns"][0]["at"] = (NOW + timedelta(seconds=1)).isoformat()
    if change == "unready":
        a["ready"] = False
    selection = select_plans(snapshot, state, NOW)
    assert selection["plans"] == []
    assert any(reason in r for r in selection["evaluations"][0]["reasons"])


@pytest.mark.parametrize(
    "kind,spot", [("CE", "54949"), ("CE", "55051"), ("CE", "54999"), ("PE", "55051"), ("PE", "54949"), ("PE", "55001")]
)
def test_entry_recheck_rejects_invalidated_untriggered_or_chased_price(kind, spot):
    snapshot, state, quote = fixtures(kind)
    with pytest.raises(ValueError, match="trigger"):
        validate_plan(decision(snapshot), snapshot, state, quote, {"at": NOW.isoformat(), "spot": spot}, NOW)


@pytest.mark.parametrize(
    "failure",
    [
        "unknown",
        "tampered",
        "expired",
        "stale_underlying",
        "older_underlying",
        "premium",
        "depth",
        "cash",
        "limits",
        "spread",
        "symbol",
        "strategy",
    ],
)
def test_independent_execution_rejects_invalid_plans(failure):
    snapshot, state, quote = fixtures()
    d = decision(snapshot)
    now = NOW
    sample = {"at": NOW.isoformat(), "spot": "55000"}
    if failure == "unknown":
        d["plan_id"] = "invented"
    if failure == "tampered":
        snapshot["strategy_selection"]["plans"][0]["quantity"] = 600
    if failure == "expired":
        now += timedelta(seconds=91)
    if failure == "stale_underlying":
        sample["at"] = (NOW - timedelta(seconds=121)).isoformat()
    if failure == "older_underlying":
        sample["at"] = (NOW - timedelta(seconds=1)).isoformat()
    if failure == "premium":
        quote.update(bid="102", ask="103")
    if failure == "depth":
        quote["ask_size"] = 30
    if failure == "cash":
        state["cash"] = "100"
    if failure == "limits":
        state["loss_pct"] = "6"
    if failure == "spread":
        quote["bid"] = "90"
    if failure == "symbol":
        d["symbol"] = "wrong"
    if failure == "strategy":
        d["strategy"] = "reversal"
    with pytest.raises(ValueError):
        validate_plan(d, snapshot, state, quote, sample, now)


def test_contract_selection_is_deterministic_and_respects_daily_risk_budget():
    snapshot, state, quote = fixtures()
    other = deepcopy(snapshot["candidates"][0])
    other["symbol"] = "FURTHER"
    other["strike"] = "55500"
    snapshot["candidates"].insert(0, other)
    selection = select_plans(snapshot, state, NOW)
    assert selection["plans"][0]["symbol"] != "FURTHER"
    state["realized_pnl"] = "-4999"
    assert quantity_for(state, other, quote) == 0
    assert select_plans(snapshot, state, NOW)["plans"] == []


def test_no_ai_plan_when_price_has_already_moved_beyond_chase_bound():
    snapshot, state, _ = fixtures()
    snapshot["spot"] = "55200"
    selection = select_plans(snapshot, state, NOW)
    assert selection["plans"] == []
    assert "chase bounds" in selection["evaluations"][0]["reasons"][0]


def test_underlying_exit_requires_fresh_post_entry_candle_and_correct_direction():
    for kind, spot in [("CE", "54900"), ("PE", "55100")]:
        snapshot, _, _ = fixtures(kind)
        pos = {"entry_plan": snapshot["strategy_selection"]["plans"][0], "entered_at": NOW.isoformat()}
        assert underlying_exit(pos, {"at": NOW.isoformat(), "spot": spot}, NOW)
        assert not underlying_exit(pos, {"at": NOW.isoformat(), "spot": "55000"}, NOW)
        assert not underlying_exit(pos, {"at": (NOW - timedelta(seconds=1)).isoformat(), "spot": spot}, NOW)
        assert not underlying_exit(pos, None, NOW)


def test_bounded_plans_do_not_expand_contract_universe_or_trial_budget():
    snapshot, state, _ = fixtures()
    for p in PLAYBOOKS[1:3]:
        pattern = deepcopy(snapshot["chart_analysis"]["patterns"][0])
        pattern.update(name=p["pattern"], id=p["pattern"] + "_bullish")
        snapshot["chart_analysis"]["patterns"].append(pattern)
    snapshot["candidates"] *= 6
    snapshot["strategy_selection"] = select_plans(snapshot, state, NOW)
    assert len(snapshot["strategy_selection"]["plans"]) == 3
    assert len(request_body(snapshot)) < 20000
