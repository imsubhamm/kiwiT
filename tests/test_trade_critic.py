import asyncio
import json
from copy import deepcopy

import pytest
from test_context_analyst import scenario
from test_llm_gateway import Audit, Provider, reply

from kiwit.llm.critic import TradeCritic, benchmark_critic, proposal_for_risk
from kiwit.llm.gateway import LLMGateway


def candidate():
    data = scenario()
    data.update(
        proposal={
            "candidate_id": "candidate",
            "direction": "LONG",
            "entry": "100",
            "stop": "99",
            "target": "102",
            "quantity": 10,
            "confidence": 0.8,
            "thesis": "Supplied trend setup",
        },
        features={"rsi": 55.0, "atr": 1.0, "ema_distance": 0.002},
        historical_similarity="Prior completed observations show mixed outcomes.",
        context=json.loads(reply().content),
    )
    return data


def critic(responses):
    provider = Provider(responses)
    return TradeCritic(LLMGateway({"deepseek": provider}, Audit())), provider


def test_approve_is_bound_risk_review_only():
    service, _ = critic([reply(True)])
    data = candidate()
    original = deepcopy(data)
    result = asyncio.run(service.critique(data))
    assert data == original
    assert result["status"] == "RISK_REVIEW_REQUIRED"
    assert result["execution_allowed"] is False
    assert proposal_for_risk(result, data["proposal"], snapshot_fingerprint="snapshot") == data["proposal"]
    changed = {**data["proposal"], "quantity": 20}
    assert proposal_for_risk(result, changed, snapshot_fingerprint="snapshot") is None
    assert proposal_for_risk(result, data["proposal"], snapshot_fingerprint="other") is None


@pytest.mark.parametrize(
    "extra",
    [
        {"verdict": "REJECT"},
        {"verdict": "UNCERTAIN"},
        {"quantity": 999},
        {"warnings": ["ADVERSE_HISTORY"]},
        {"confidence": 0.3},
    ],
)
def test_reject_uncertain_or_mutation_cannot_progress(extra):
    service, _ = critic([reply(True, **extra)])
    data = candidate()
    result = asyncio.run(service.critique(data))
    assert result["status"] == "NO_TRADE"
    assert proposal_for_risk(result, data["proposal"], snapshot_fingerprint="snapshot") is None


def test_missing_evidence_and_invalid_proposal_do_not_call_model():
    for field in ("context", "historical_similarity", "features"):
        service, provider = critic([])
        data = candidate()
        del data[field]
        result = asyncio.run(service.critique(data))
        assert result["status"] == "NO_TRADE"
        assert not provider.calls
    service, provider = critic([])
    data = candidate()
    data["proposal"]["stop"] = "103"
    assert asyncio.run(service.critique(data))["status"] == "NO_TRADE"
    assert not provider.calls


def test_outcomes_hidden_and_metrics():
    service, provider = critic([reply(True, verdict="REJECT"), reply(True, verdict="REJECT"), reply(True), reply(True)])
    first, second = candidate(), candidate()
    second["as_of"] = "2026-09-10T10:30:00+05:30"
    cases = [
        {"input": first, "outcome": {"net_return": -0.01, "exit_at": "2026-09-10T10:15:00+05:30"}},
        {"input": second, "outcome": {"net_return": 0.02, "exit_at": "2026-09-10T10:45:00+05:30"}},
    ]
    report = asyncio.run(benchmark_critic(service, cases))
    assert report["losing_trades_rejected"] == 1
    assert report["winning_trades_falsely_rejected"] == 0
    assert report["net_expectancy"] == 0.02
    assert report["consistent_scenario_fraction"] == 1
    assert report["unit_equity_max_drawdown"] == 0
    assert all("net_return" not in call["user"] and "exit_at" not in call["user"] for call in provider.calls)
