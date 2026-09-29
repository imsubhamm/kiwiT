from copy import deepcopy
from datetime import timedelta

import pytest
from test_playbooks import NOW, fixtures

from kiwit.options_policy import decision_event, reconsider_event


def event():
    return decision_event(fixtures()[0])


@pytest.mark.parametrize("field,value", [
    ("pattern_at", (NOW + timedelta(hours=1)).isoformat()),
    ("underlying_trigger", "55001"),
    ("underlying_invalidation", "54949"),
    ("underlying_max_chase", "55026"),
])
def test_new_occurrence_and_material_bounds_change_key(field, value):
    snapshot, _, _ = fixtures()
    original = decision_event(snapshot)
    snapshot["strategy_selection"]["plans"][0][field] = value
    changed = decision_event(snapshot)
    assert changed["key"] != original["key"]
    assert reconsider_event(changed, [], NOW)["new"]


def test_polling_timestamps_prices_format_and_order_are_not_events():
    snapshot, _, _ = fixtures()
    plans = snapshot["strategy_selection"]["plans"]
    plans.append({**plans[0], "symbol": "OTHER"})
    original = decision_event(snapshot)
    for plan in plans:
        plan.update(id="regenerated", created_at="later", expires_at="later", planned_fill="102")
        plan["underlying_trigger"] = "55000.00"
    plans.reverse()
    assert decision_event(snapshot)["key"] == original["key"]


@pytest.mark.parametrize("status,action,reason", [
    ("applied", "HOLD", "HOLD_RECONSIDERATION"),
    ("rejected", "BUY", "RETRY_REJECTED"),
    ("failed", None, "RETRY_FAILED"),
    ("interrupted", None, "RETRY_INTERRUPTED"),
])
def test_reconsideration_has_cooldown_and_hard_attempt_bound(status, action, reason):
    attempts = [(status, NOW, action)]
    early = reconsider_event(event(), attempts, NOW + timedelta(seconds=119))
    assert not early["new"] and early["reason"] == "RECONSIDERATION_COOLDOWN"
    due = reconsider_event(event(), attempts, NOW + timedelta(seconds=120))
    assert due["new"] and due["reason"] == reason
    attempts = [(status, NOW + timedelta(seconds=120), action)] + attempts
    assert reconsider_event(event(), attempts, NOW + timedelta(seconds=240))["new"]
    attempts = [(status, NOW + timedelta(seconds=240), action)] + attempts
    exhausted = reconsider_event(event(), attempts, NOW + timedelta(hours=1))
    assert not exhausted["new"] and exhausted["reason"] == "EVENT_ATTEMPT_LIMIT"


@pytest.mark.parametrize("status,action", [("reserved", None), ("completed", "HOLD"), ("applied", "BUY")])
def test_pending_or_successful_execution_not_retried(status, action):
    assert not reconsider_event(event(), [(status, NOW, action)], NOW + timedelta(hours=1))["new"]


def test_disappearance_reappearance_retains_attempt_history():
    snapshot, _, _ = fixtures()
    first = decision_event(snapshot)
    absent = deepcopy(snapshot)
    absent["strategy_selection"]["plans"] = []
    assert not reconsider_event(decision_event(absent), [], NOW)["new"]
    # The same occurrence can return after a quote/gate recovers; it is eligible
    # for bounded reconsideration, but flickering does not reset the attempt cap.
    attempts = [("applied", NOW, "HOLD")]
    assert decision_event(snapshot)["key"] == first["key"]
    assert not reconsider_event(first, attempts, NOW + timedelta(seconds=30))["new"]
    assert reconsider_event(first, attempts, NOW + timedelta(seconds=120))["new"]
    assert not reconsider_event(first, attempts * 3, NOW + timedelta(seconds=300))["new"]


def test_position_hold_does_not_create_periodic_paid_monitoring():
    trigger = decision_event({"position": {"id": "p", "mark": "100", "stop": "99", "target": "120"}})
    assert not reconsider_event(trigger, [("applied", NOW, "HOLD")], NOW + timedelta(minutes=3))["new"]
