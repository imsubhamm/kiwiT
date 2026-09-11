from copy import deepcopy

import pytest

from kiwit.evaluation_dashboard import evaluate, from_backtest, render, summarize
from kiwit.ml.datasets import _hash


def run():
    return {
        "run_id": "fixture",
        "mode": "backtest",
        "models": ["model-1"],
        "prompt_version": "prompt-1",
        "config_version": "config-1",
        "initial_equity": "100",
        "equity_curve": [
            {"at": "2026-09-01T04:00:00+00:00", "equity": "110"},
            {"at": "2026-09-01T04:01:00+00:00", "equity": "99"},
        ],
        "rows": [
            {
                "at": "2026-09-01T04:00:00+00:00",
                "status": "SUBMITTED",
                "net_pnl": "10",
                "confidence": 0.8,
                "strategy": "trend",
                "regime": "UPTREND",
                "llm_verdict": "APPROVE",
                "candidate_outcome": "10",
                "latency_ms": 100,
                "llm_cost_usd": ".01",
            },
            {
                "at": "2026-09-01T04:01:00+00:00",
                "status": "SUBMITTED",
                "net_pnl": "-5",
                "confidence": 0.6,
                "strategy": "trend",
                "regime": "UPTREND",
            },
            {
                "at": "2026-09-01T04:02:00+00:00",
                "status": "NO_TRADE",
                "llm_verdict": "REJECT",
                "candidate_outcome": "-3",
            },
        ],
    }


def test_metrics_degradation_and_missing_evidence():
    report = summarize(run())
    assert report["metrics"]["net_pnl"] == "5"
    assert report["metrics"]["profit_factor"] == "2"
    assert report["metrics"]["win_rate"] == 0.5
    assert report["metrics"]["maximum_drawdown"] == "0.1"
    assert report["metrics"]["no_trade"] == 1
    assert report["degradation"]["expectancy_declined"] is True
    assert report["calibration"]["brier_score"] == pytest.approx(0.2)
    assert report["rejection_outcomes"]["avoided_losses"] == 1
    assert report["telemetry"]["cost_coverage"] == 1
    assert report["breakdowns"]["time_of_day"]["09:00 IST"]["closed_trades"] == 2
    assert "NOT_VALIDATED" in report["validation"]


def test_filters_modes_duplicates_and_html_escaping():
    first = run()
    second = deepcopy(first)
    second.update(run_id="<script>alert(1)</script>", mode="paper", prompt_version="p2")
    assert len(evaluate([first, second])["runs"]) == 2
    assert len(evaluate([first, second], mode="paper", prompt="p2", model="model-1", config="config-1")["runs"]) == 1
    assert evaluate([first], model="missing")["runs"] == []
    assert "<script>" not in render(evaluate([second]))
    with pytest.raises(ValueError, match="Duplicate"):
        evaluate([first, first])


def test_empty_invalid_and_unknown_metrics():
    data = run()
    data["rows"] = []
    result = summarize(data)
    assert result["metrics"]["win_rate"] is None
    assert result["degradation"]["expectancy_declined"] is None
    assert result["calibration"]["brier_score"] is None
    data = run()
    data["rows"][0]["net_pnl"] = "NaN"
    with pytest.raises(ValueError):
        summarize(data)
    data = run()
    data["rows"].reverse()
    with pytest.raises(ValueError, match="timestamps"):
        summarize(data)


def test_verified_backtest_adapter():
    body = {
        "version": "unified-replay-v1",
        "model_bindings": {"trend": "abc"},
        "configuration": {"test": True},
        "portfolio": {"orders": {}, "metadata": {"initial_capital": "100"}},
        "equity_curve": [],
        "decisions": [{"at": "2026-09-01T04:00:00+00:00", "status": "NO_TRADE", "meta": {"selected_candidate": None}}],
    }
    artifact = {**body, "fingerprint": _hash(body)}
    converted = from_backtest(artifact)
    assert converted["mode"] == "backtest"
    assert converted["prompt_version"] == "UNRECORDED"
    assert summarize(converted)["telemetry"]["cost_coverage"] == 0
    artifact["version"] = "changed"
    with pytest.raises(ValueError):
        from_backtest(artifact)
