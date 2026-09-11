import asyncio
from copy import deepcopy

import pytest
from test_llm_gateway import Audit, Provider, reply

from kiwit.llm.context import ContextAnalyst, context_confidence_for_policy, evaluate_scenarios
from kiwit.llm.gateway import LLMGateway


def scenario():
    return {
        "instrument": "BANKNIFTY",
        "as_of": "2026-09-10T10:00:00+05:30",
        "snapshot_fingerprint": "snapshot",
        "quant": {
            "regime": "UPTREND",
            "trend_probability": 0.8,
            "breakout_probability": 0.7,
            "reversion_probability": 0.2,
            "volatility": 0.002,
            "session_fraction": 0.2,
        },
        "evidence": [
            {
                "source_id": "s1",
                "instrument": "MARKET",
                "published_at": "2026-09-10T09:00:00+05:30",
                "available_at": "2026-09-10T09:01:00+05:30",
                "category": "CENTRAL_BANK",
                "text": "Supplied RBI bulletin",
            }
        ],
    }


def analyst(replies):
    provider = Provider(replies)
    return ContextAnalyst(LLMGateway({"deepseek": provider}, Audit())), provider


def test_context_signals_no_trade_authority():
    service, provider = analyst([reply()])
    result = asyncio.run(service.analyze(scenario()))
    assert result["candidate_effect"] == "NO_ADDITIONAL_VETO"
    assert result["gateway"]["execution_allowed"] is False
    assert context_confidence_for_policy(result, "snapshot") == 0.8
    assert context_confidence_for_policy(result, "different") == 0
    assert "BANKNIFTY" in provider.calls[0]["user"]
    assert "CENTRAL_BANK" in provider.calls[0]["user"]


@pytest.mark.parametrize("change", ["future", "unrelated", "missing", "naive", "extra"])
def test_invalid_evidence_fails_closed_before_provider(change):
    data = scenario()
    if change == "future":
        data["evidence"][0]["available_at"] = "2026-09-11T09:01:00+05:30"
    elif change == "unrelated":
        data["evidence"][0]["instrument"] = "OTHER"
    elif change == "missing":
        data["evidence"] = []
    elif change == "naive":
        data["as_of"] = "2026-09-10T10:00:00"
    else:
        data["quantity"] = 100
    service, provider = analyst([])
    result = asyncio.run(service.analyze(data))
    assert result["candidate_effect"] == "BLOCK"
    assert not provider.calls


@pytest.mark.parametrize(
    "extra", [{"event_risk": "HIGH"}, {"quant_conflict": "YES"}, {"market_context": "UNKNOWN"}, {"confidence": 0.3}]
)
def test_context_veto(extra):
    service, _ = analyst([reply(**extra)])
    result = asyncio.run(service.analyze(scenario()))
    assert result["candidate_effect"] == "BLOCK"
    assert context_confidence_for_policy(result, "snapshot") == 0


def test_frozen_scenario_metrics():
    service, _ = analyst(
        [
            reply(event_risk="HIGH", quant_conflict="YES"),
            reply(event_risk="HIGH", quant_conflict="YES"),
            reply(),
            reply(),
        ]
    )
    data = [
        {"input": scenario(), "event_risk": "HIGH", "quant_conflict": "YES"},
        {"input": scenario(), "event_risk": "LOW", "quant_conflict": "NO"},
    ]
    original = deepcopy(data)
    report = asyncio.run(evaluate_scenarios(service, data))
    assert data == original
    assert report["event_risk_recall"] == 1
    assert report["false_alarm_rate"] == 0
    assert report["quant_conflict_accuracy"] == 1
    assert report["consistent_scenario_fraction"] == 1
    assert report["blocked_fraction"] == 0.5
    assert report["semantic_hallucination_rate"] is None
    assert report["realized_trading_impact"] is None
