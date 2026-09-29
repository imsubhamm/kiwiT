"""KIW-42: actionable entry blockers outrank the absence of a setup."""
# ruff: noqa: F811
from itertools import permutations

import pytest
from test_banknifty import NOW, db, desk, warm  # noqa: F401

from kiwit.options_policy import entry_blocker_detail, entry_gate


def test_simultaneous_blockers_have_stable_display_order():
    expected = ["SESSION_PNL_LIMIT", "DAILY_ENTRY_CAP", "EVENT_CALENDAR_UNAVAILABLE",
                "POST_EXIT_COOLDOWN", "NO_ELIGIBLE_PLAN"]
    for reasons in permutations(expected):
        detail = entry_blocker_detail({"reason_codes": list(reasons)})
        assert [part.split("[")[-1].rstrip("]") for part in detail.split("; ") if "[" in part] == expected[:-1]
    state = {"amount": "100000", "loss_pct": "5", "profit_pct": "10",
             "realized_pnl": "-5000", "entries": 10, "last_exit": NOW.isoformat()}
    assert entry_gate(state, NOW, [], {"coverage": "unconfigured"})["reason_codes"] == expected


@pytest.mark.parametrize("context,code,text", [
    ({"coverage": "unconfigured"}, "EVENT_CALENDAR_UNAVAILABLE", "Event calendar unavailable"),
    ({"coverage": "invalid"}, "EVENT_CALENDAR_INVALID", "Event calendar invalid"),
    ({"coverage": "configured", "risk": "high"}, "HIGH_IMPACT_EVENT_WINDOW", "High-impact event window"),
])
def test_calendar_reason_precedes_no_plan_and_clears(context, code, text):
    state = {"amount": "100000", "loss_pct": "5", "profit_pct": "10"}
    gate = entry_gate(state, NOW, [], context)
    assert gate["reason_codes"] == [code, "NO_ELIGIBLE_PLAN"]
    assert entry_blocker_detail(gate).startswith(text)
    recovered = entry_gate(state, NOW, [], {"coverage": "configured", "risk": "clear"})
    assert recovered["reason_codes"] == ["NO_ELIGIBLE_PLAN"]
    assert code not in entry_blocker_detail(recovered)
    assert entry_blocker_detail(entry_gate(state, NOW, [{}], {"coverage": "configured", "risk": "clear"})) is None


@pytest.mark.parametrize("coverage,risk,code", [
    ("unconfigured", "unknown", "EVENT_CALENDAR_UNAVAILABLE"),
    ("invalid", "unknown", "EVENT_CALENDAR_INVALID"),
    ("configured", "high", "HIGH_IMPACT_EVENT_WINDOW"),
])
@pytest.mark.parametrize("has_setup", [False, True])
def test_session_displays_calendar_blocker_and_recovers(desk, monkeypatch, coverage, risk, code, has_setup):
    service, market, analyst, _clock = desk
    context = {"coverage": coverage, "risk": risk, "events": []}
    monkeypatch.setattr("kiwit.banknifty.event_context", lambda _now: context)
    if not has_setup:
        original = market.snapshot

        def no_setup(now):
            snapshot = original(now)
            snapshot["chart_analysis"]["patterns"] = []
            return snapshot

        market.snapshot = no_setup
    state = warm(desk)
    assert code in state["detail"]
    assert "NO_ELIGIBLE_PLAN" not in state["detail"]
    assert bool(state["strategy_selection"]["shadow_plans"]) is has_setup
    assert analyst.calls == 0
    context.update(coverage="configured", risk="clear")
    service.run_once()
    recovered = service.status()["session"]
    assert code not in recovered["detail"]
    if not has_setup:
        assert recovered["detail"].startswith("No eligible entry plan")
    else:
        assert recovered["position"] is not None


@pytest.mark.parametrize('coverage,detail,code,label', [
    ('unconfigured', 'CALENDAR_PATH_UNCONFIGURED', 'EVENT_CALENDAR_UNAVAILABLE', 'Event calendar unavailable'),
    ('invalid', 'CALENDAR_JSON_INVALID', 'EVENT_CALENDAR_INVALID', 'Event calendar invalid'),
    ('invalid', 'CALENDAR_STALE', 'EVENT_CALENDAR_INVALID', 'Event calendar invalid'),
    ('invalid', 'CALENDAR_DAY_NOT_COVERED', 'EVENT_CALENDAR_INVALID', 'Event calendar invalid'),
])
def test_scan_persists_primary_blocker_without_any_setup_and_clears_it(monkeypatch, coverage, detail, code, label):
    """Exercise the real scan/selector with an in-memory persistence boundary."""
    from datetime import timedelta
    from unittest.mock import MagicMock

    from test_banknifty import Analyst, BankNiftyService, Market

    monkeypatch.setenv("KIWIT_BANKNIFTY_AI_ENABLED", "true")
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    state = {"day": str(NOW.date()), "state": "running", "position": None,
             "amount": "100000", "cash": "100000", "loss_pct": "5", "profit_pct": "10",
             "realized_pnl": "0", "entries": 0,
             "history": [{"at": (NOW - timedelta(minutes=i)).isoformat(), "spot": "55000"}
                         for i in range(4, -1, -1)]}
    market = Market()
    snapshot = market.snapshot(NOW)
    snapshot["chart_analysis"]["patterns"] = []
    market.snapshot = lambda _now: dict(snapshot)
    service = BankNiftyService(None, None, market=market, analyst=Analyst(), clock=lambda: NOW)
    service.store = MagicMock()
    service.store.latest.return_value = state
    service.store.learning_context.return_value = {}
    connection = service.store.locked.return_value.__enter__.return_value
    connection.execute.return_value.fetchone.return_value = None
    connection.execute.return_value.fetchall.return_value = []
    service._monitor = lambda: state
    service._process_daily_report = lambda *_: None
    context = {"coverage": coverage, "risk": "unknown", "reason_code": detail}
    monkeypatch.setattr("kiwit.banknifty.event_context", lambda _: context)
    assert service.run_once()["reason_codes"] == [code, "NO_ELIGIBLE_PLAN"]
    assert state["detail"].startswith(label)
    assert detail in state["detail"]
    assert "NO_ELIGIBLE_PLAN" not in state["detail"]
    assert state["strategy_selection"]["shadow_plans"] == []
    context.update(coverage="configured", risk="clear")
    assert service.run_once()["reason_codes"] == ["NO_ELIGIBLE_PLAN"]
    assert state["detail"].startswith("No eligible entry plan")
    assert "EVENT_CALENDAR" not in state["detail"]
    assert service.analyst.calls == 0
    assert service.store.save.call_args.args[1]["detail"] == state["detail"]
