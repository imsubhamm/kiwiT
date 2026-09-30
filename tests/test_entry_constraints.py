from copy import deepcopy
from datetime import timedelta

import pytest
from test_playbooks import NOW, decision, fixtures

from kiwit.options_policy import RECONSIDER_SECONDS, reconsider_event
from kiwit.options_risk import quantity_for, sizing_diagnostics
from kiwit.playbooks import EntryQuoteRejection, select_plans, validate_plan


def _contract(snapshot):
    return snapshot["candidates"][0]


def test_sufficient_capital_names_a_binding_limit_and_matches_quantity_authority():
    snapshot, state, quote = fixtures()
    contract = _contract(snapshot)
    diagnostic = sizing_diagnostics(state, contract, quote)
    assert diagnostic["whole_lot_quantity"] == quantity_for(state, contract, quote) > 0
    assert diagnostic["binding_constraint"] in {item["code"] for item in diagnostic["constraints"]}
    assert diagnostic["guaranteed_loss_cap"] is False
    assert "not a guaranteed loss cap" in diagnostic["note"]
    assert "sizing" not in snapshot["strategy_selection"]["evaluations"][0]


def test_insufficient_cash_binds_before_a_lot_can_be_bought():
    snapshot, state, quote = fixtures()
    state["cash"] = "10"
    diagnostic = sizing_diagnostics(state, _contract(snapshot), quote)
    assert diagnostic["whole_lot_quantity"] == 0
    assert diagnostic["binding_constraint"] == "fee_reserve"
    assert "cash" in diagnostic["binding_constraints"]
    selection = select_plans(snapshot, state, NOW)
    assert selection["plans"] == []
    sizing = selection["evaluations"][0]["sizing"][0]
    assert sizing["binding_constraint"] == "fee_reserve"
    assert "binding fee_reserve" in selection["evaluations"][0]["reasons"][0]


def test_realized_loss_binds_the_remaining_daily_risk_budget():
    snapshot, state, quote = fixtures()
    state["realized_pnl"] = "-4999"
    diagnostic = sizing_diagnostics(state, _contract(snapshot), quote)
    assert diagnostic["whole_lot_quantity"] == 0
    assert diagnostic["binding_constraint"] == "fee_reserve"
    assert "remaining_daily_risk" in diagnostic["binding_constraints"]


def test_displayed_depth_and_freeze_limits_bind_without_raising_capital_need():
    snapshot, state, quote = fixtures()
    contract = _contract(snapshot)
    shallow = deepcopy(quote)
    shallow["ask_size"] = 10
    depth = sizing_diagnostics(state, contract, shallow)
    assert depth["whole_lot_quantity"] == 0
    assert depth["binding_constraint"] == "ask_depth"
    assert depth["lot_blocks_whole_lot"] is True

    frozen = deepcopy(contract)
    frozen["freeze"] = 20
    freeze = sizing_diagnostics(state, frozen, quote)
    assert freeze["whole_lot_quantity"] == 0
    assert freeze["binding_constraint"] == "freeze_limit"


def test_quote_rejections_keep_separate_codes_and_recover_on_a_fresh_quote():
    snapshot, state, quote = fixtures()
    plan = snapshot["strategy_selection"]["plans"][0]
    stale = deepcopy(quote)
    stale["stamp"] = (NOW - timedelta(seconds=91)).isoformat()
    stale["bid"] = "80"
    stale["ask"] = "120"
    with pytest.raises(EntryQuoteRejection) as rejected:
        validate_plan(decision(snapshot), snapshot, state, stale, {"at": NOW.isoformat(), "spot": "55000"}, NOW)
    assert rejected.value.primary_reason_code == "QUOTE_STALE"
    assert rejected.value.reason_codes == ["QUOTE_STALE", "QUOTE_SPREAD", "ENTRY_PRICE_LIMIT"]
    assert rejected.value.failures[0]["threshold"] == "90"
    assert rejected.value.failures[1]["threshold"] == "0.02"
    assert rejected.value.failures[2]["threshold"] == plan["max_fill"]
    assert validate_plan(
        decision(snapshot), snapshot, state, quote, {"at": NOW.isoformat(), "spot": "55000"}, NOW
    ) == plan


def test_price_limit_is_reported_alone_when_the_quote_is_fresh():
    snapshot, state, quote = fixtures()
    expensive = deepcopy(quote)
    expensive.update(bid="140", ask="141")
    with pytest.raises(EntryQuoteRejection) as rejected:
        validate_plan(decision(snapshot), snapshot, state, expensive, {"at": NOW.isoformat(), "spot": "55000"}, NOW)
    assert rejected.value.reason_codes == ["ENTRY_PRICE_LIMIT"]


def test_rejected_quote_recheck_is_reconsidered_after_the_existing_cooldown():
    trigger = {"call": True, "kind": "ELIGIBLE_PLAN_SET_CHANGED", "key": "same-setup"}
    attempts = [("rejected", NOW, "BUY")]
    waiting = reconsider_event(trigger, attempts, NOW + timedelta(seconds=RECONSIDER_SECONDS - 1))
    due = reconsider_event(trigger, attempts, NOW + timedelta(seconds=RECONSIDER_SECONDS))
    assert waiting["new"] is False and waiting["reason"] == "RECONSIDERATION_COOLDOWN"
    assert due["new"] is True and due["reason"] == "RETRY_REJECTED"
