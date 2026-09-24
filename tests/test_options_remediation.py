import json
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal as D

import pytest
from test_playbooks import NOW, fixtures

from kiwit.options_ai import compact_decision_snapshot
from kiwit.options_evaluation import cost_reconciliation, exit_matrix, rule_matrix
from kiwit.options_events import event_context
from kiwit.options_market import executable_quote
from kiwit.options_policy import decision_event, entry_gate
from kiwit.options_risk import BROKER_COST_VERSION, cost_breakdown, exit_levels, fill_price, quantity_for
from kiwit.playbooks import PLAYBOOKS, select_plans


def test_sizing_restores_quarter_allocation_and_one_percent_planned_risk():
    snapshot, state, quote = fixtures()
    contract = snapshot["candidates"][0]
    quantity = quantity_for(state, contract, quote)
    fill = fill_price(quote, contract, True)
    assert fill * quantity + cost_breakdown(fill * quantity, "buy")["total"] <= D(state["amount"]) * D(".25")
    stop_loss = fill * quantity * D(state["loss_pct"]) / 100
    costs = cost_breakdown(fill * quantity, "buy")["total"] + cost_breakdown(fill * quantity, "sell")["total"]
    assert stop_loss + costs <= D(state["amount"]) * D(".01")


def test_current_cost_schedule_has_side_specific_components():
    buy = cost_breakdown(D(100000), "buy")
    sell = cost_breakdown(D(100000), "sell")
    assert buy["version"] == sell["version"] == BROKER_COST_VERSION
    assert buy["stamp"] > 0 and buy["stt"] == 0
    assert sell["stt"] > 0 and sell["stamp"] == 0
    assert sell["total"] > buy["total"]


def test_entry_gates_run_before_ai_and_keep_shadow_reason_codes():
    snapshot, state, _ = fixtures()
    state.update(entries=10, last_exit=(NOW - timedelta(seconds=60)).isoformat())
    gate = entry_gate(state, NOW, snapshot["strategy_selection"]["plans"])
    assert not gate["allowed"]
    assert gate["reason_codes"] == ["DAILY_ENTRY_CAP", "POST_EXIT_COOLDOWN"]
    assert gate["cooldown_remaining_seconds"] == 240


def test_decision_event_ignores_plan_timestamp_and_id_churn():
    snapshot, _, _ = fixtures()
    first = decision_event(snapshot)
    changed = deepcopy(snapshot)
    changed["strategy_selection"]["plans"][0].update(id="new", created_at=(NOW + timedelta(minutes=1)).isoformat())
    assert decision_event(changed)["key"] == first["key"]


def test_chase_and_exhaustion_protection():
    snapshot, state, _ = fixtures()
    snapshot["spot"] = "55026"
    assert not select_plans(snapshot, state, NOW)["plans"]
    snapshot["spot"] = "55020"
    snapshot["chart_analysis"]["timeframes"]["5m"]["rsi14"] = 80
    selection = select_plans(snapshot, state, NOW)
    assert not selection["plans"]
    assert any("exhaustion" in reason for reason in selection["evaluations"][0]["reasons"])


def test_provider_option_fields_are_retained_with_quote():
    payload = {"bid_price": 100, "offer_price": 101, "bid_quantity": 60, "offer_quantity": 60,
               "last_trade_time": int(NOW.timestamp()), "open_interest": 1234, "volume": 987,
               "implied_volatility": "18.5", "delta": ".52"}
    fields = executable_quote(payload, NOW)["market_fields"]
    assert fields == {"open_interest": "1234", "volume": "987", "implied_volatility": "18.5", "delta": "0.52"}


def test_ai_receives_available_option_fields_and_bounded_premium_history():
    snapshot, _, _ = fixtures()
    snapshot["candidates"][0]["quote"]["market_fields"] = {"implied_volatility": "18.5", "delta": ".52"}
    tracked = deepcopy(snapshot["candidates"][0])
    tracked.update(symbol="OLD", selection_eligible=False)
    snapshot["candidates"].append(tracked)
    snapshot["premium_history"] = [{"at": NOW.isoformat(), "symbol": "X", "bid": str(i), "ask": str(i + 1)}
                                    for i in range(60)]
    compact = compact_decision_snapshot(snapshot)
    assert compact["candidates"][0]["quote"]["market_fields"]["delta"] == ".52"
    assert all(candidate["symbol"] != "OLD" for candidate in compact["candidates"])
    assert len(compact["premium_history"]) == 40


def test_verified_event_calendar_blocks_only_configured_high_impact_window(tmp_path, monkeypatch):
    path = tmp_path / "events.json"
    path.write_text(json.dumps({"version": "fixture", "as_of": NOW.isoformat(), "owner": "test",
                                "source_reference": "https://example.invalid/fixture", "events": [
        {"at": (NOW + timedelta(minutes=30)).isoformat(), "name": "Verified fixture", "impact": "high"}]}))
    monkeypatch.setenv("KIWIT_OPTIONS_EVENT_CALENDAR", str(path))
    context = event_context(NOW)
    assert context["coverage"] == "configured" and context["risk"] == "high"
    snapshot, state, _ = fixtures()
    snapshot["event_context"] = context
    assert select_plans(snapshot, state, NOW)["plans"] == []


def test_missing_event_calendar_fails_closed_before_ai(monkeypatch):
    monkeypatch.delenv("KIWIT_OPTIONS_EVENT_CALENDAR", raising=False)
    context = event_context(NOW)
    snapshot, state, _ = fixtures()
    snapshot["event_context"] = context
    assert select_plans(snapshot, state, NOW)["plans"] == []
    gate = entry_gate(state, NOW, [], context)
    assert not gate["allowed"] and "EVENT_CALENDAR_UNAVAILABLE" in gate["reason_codes"]


def test_playbook_exit_is_cost_aware_and_not_universal():
    first, state, _ = fixtures(playbook=PLAYBOOKS[0])
    reversal, _, _ = fixtures(playbook=PLAYBOOKS[3])
    a, b = first["strategy_selection"]["plans"][0], reversal["strategy_selection"]["plans"][0]
    assert a["live_exit"] != b["live_exit"]
    contract = first["candidates"][0]
    levels = exit_levels(state, a["live_exit"], D(a["planned_fill"]), a["quantity"], contract)
    assert levels["net_reward_at_target"] > 0
    assert levels["net_reward_r"] >= D(a["live_exit"]["reward_r"])


def test_cost_dominated_contract_is_rejected():
    snapshot, state, _ = fixtures()
    contract = snapshot["candidates"][0]
    with pytest.raises(ValueError, match="costs consume"):
        exit_levels(state, PLAYBOOKS[0]["live_exit"], D(5), contract["lot"], contract)


def test_rule_exit_and_actual_cost_matrices_use_recorded_evidence():
    snapshot, _, _ = fixtures()
    snapshot["capital"] = "100000"
    contract = deepcopy(snapshot["candidates"][0])
    tape = []
    for minutes in (0, 15, 30, 45):
        item = deepcopy(contract)
        at = NOW + timedelta(minutes=minutes)
        item["quote"]["stamp"] = at.isoformat()
        tape.append({"recorded_at": at.isoformat(), "market_snapshot": {
            "candidates": [item], "spot": snapshot["spot"], "spot_at": at.isoformat()}})
    bundle = {"calls": [{"trading_date": "2026-08-26", "snapshot": snapshot,
                          "result": {"settled_at": NOW.isoformat()}}], "market_tape": tape,
              "events": [{"kind": "paper_entry", "detail": {"position": {"id": "p1"},
                                                                "entry_costs": {"total": "40"}}},
                         {"kind": "paper_exit", "detail": {"position_id": "p1",
                                                              "exit_costs": {"total": "50"}}}],
              "broker_cost_evidence": [{"position_id": "p1", "actual_cost": "87"}]}
    assert len(rule_matrix(bundle)["scenarios"]) == 9
    assert exit_matrix(bundle)["attempted"] == 3
    reconciled = cost_reconciliation(bundle)
    assert reconciled["reconciled"] == 1 and reconciled["positions"][0]["difference"] == "3"
