"""Point-in-time context analysis and frozen-scenario advisory evaluation."""

import json
from datetime import datetime
from typing import Literal

from pydantic import Field, model_validator

from kiwit.marketdata.canonical import utc
from kiwit.ml.datasets import _hash

from .gateway import Strict

VERSION = "context-analyst-v1"


class QuantSummary(Strict):
    regime: Literal["UPTREND", "DOWNTREND", "RANGE", "HIGH_VOLATILITY", "UNCERTAIN"]
    trend_probability: float = Field(ge=0, le=1)
    breakout_probability: float = Field(ge=0, le=1)
    reversion_probability: float = Field(ge=0, le=1)
    volatility: float = Field(ge=0, le=1)
    session_fraction: float = Field(ge=0, le=1)


class Evidence(Strict):
    source_id: str = Field(min_length=1, max_length=120)
    instrument: str = Field(min_length=1, max_length=120)
    published_at: str
    available_at: str
    category: Literal["NEWS", "CENTRAL_BANK", "EARNINGS", "MACRO", "REGULATORY", "GEOPOLITICAL", "CALENDAR"]
    text: str = Field(min_length=1, max_length=10000)


class ContextRequest(Strict):
    instrument: str = Field(min_length=1, max_length=120)
    as_of: str
    snapshot_fingerprint: str = Field(min_length=1, max_length=128)
    quant: QuantSummary
    evidence: list[Evidence] = Field(min_length=1, max_length=20)
    historical_similarity: str | None = Field(default=None, max_length=4000)

    @model_validator(mode="after")
    def point_in_time(self):
        at = utc(datetime.fromisoformat(self.as_of))
        ids = set()
        for item in self.evidence:
            published, available = (
                utc(datetime.fromisoformat(item.published_at)),
                utc(datetime.fromisoformat(item.available_at)),
            )
            if published > available or available > at or item.instrument not in {self.instrument, "MARKET"}:
                raise ValueError("Future or unrelated evidence")
            if item.source_id in ids:
                raise ValueError("Duplicate source")
            ids.add(item.source_id)
        return self


class ContextAnalyst:
    def __init__(self, gateway):
        self.gateway = gateway

    def _record(self, result):
        self.gateway.audit.append("context_analyst", self.gateway.redactor.clean(result))
        return result

    async def analyze(self, payload):
        try:
            request = ContextRequest.model_validate(payload)
            summary = {
                "instrument": request.instrument,
                "as_of": request.as_of,
                "quant": request.quant.model_dump(),
                "historical_similarity": request.historical_similarity,
            }
            normalized = {
                "snapshot_fingerprint": request.snapshot_fingerprint,
                "quantitative_summary": json.dumps(summary, sort_keys=True),
                "sources": [
                    {"source_id": e.source_id, "text": json.dumps(e.model_dump(), sort_keys=True)}
                    for e in request.evidence
                ],
            }
        except (ValueError, TypeError):
            # Route rejection through the gateway so prompt/provider/version and failure are still audited.
            result = await self.gateway.analyze_context({})
            return self._record(
                {
                    "analyst_version": VERSION,
                    "as_of": None,
                    "snapshot_fingerprint": None,
                    "gateway": result,
                    "candidate_effect": "BLOCK",
                    "reason_codes": ["INVALID_CONTEXT_INPUT"],
                }
            )
        result = await self.gateway.analyze_context(normalized)
        output = result["output"]
        effect = "BLOCK"
        if (
            result["status"] == "VALID"
            and output["event_risk"] == "LOW"
            and output["quant_conflict"] == "NO"
            and output["market_context"] != "UNKNOWN"
            and output["confidence"] >= 0.7
        ):
            effect = "NO_ADDITIONAL_VETO"
        return self._record(
            {
                "analyst_version": VERSION,
                "as_of": request.as_of,
                "snapshot_fingerprint": request.snapshot_fingerprint,
                "gateway": result,
                "candidate_effect": effect,
                "reason_codes": ["CONTEXT_BLOCK"] if effect == "BLOCK" else ["CONTEXT_HAS_NO_ADDITIONAL_VETO"],
            }
        )


def context_confidence_for_policy(result, snapshot_fingerprint):
    """A non-veto can satisfy only the context check, never critic/risk/execution approval."""
    if (
        result.get("analyst_version") != VERSION
        or result.get("snapshot_fingerprint") != snapshot_fingerprint
        or result.get("candidate_effect") != "NO_ADDITIONAL_VETO"
    ):
        return 0.0
    gateway = result.get("gateway", {})
    output = gateway.get("output", {})
    if (
        gateway.get("status") != "VALID"
        or output.get("event_risk") != "LOW"
        or output.get("quant_conflict") != "NO"
        or output.get("market_context") not in {"POSITIVE", "NEGATIVE", "NEUTRAL"}
    ):
        return 0.0
    value = output.get("confidence")
    return value if type(value) in (int, float) and 0.7 <= value <= 1 else 0.0


async def evaluate_scenarios(analyst, scenarios, *, repeats=2):
    """Gold labels are supplied by reviewers; no live orders or outcome fetching."""
    if type(repeats) is not int or not 1 <= repeats <= 10 or not scenarios:
        raise ValueError("Nonempty scenarios and 1-10 repeats required")
    rows = []
    for scenario in scenarios:
        if (
            set(scenario) != {"input", "event_risk", "quant_conflict"}
            or scenario["event_risk"] not in {"LOW", "HIGH"}
            or scenario["quant_conflict"] not in {"YES", "NO"}
        ):
            raise ValueError("Invalid gold scenario")
        frozen = json.loads(json.dumps(scenario, sort_keys=True, allow_nan=False))
        runs = [await analyst.analyze(frozen["input"]) for _ in range(repeats)]
        rows.append(
            {
                "scenario_fingerprint": _hash(frozen),
                "expected_event_risk": frozen["event_risk"],
                "expected_quant_conflict": frozen["quant_conflict"],
                "runs": runs,
            }
        )
    pairs = [(row, run) for row in rows for run in row["runs"]]
    valid = [(row, run) for row, run in pairs if run["gateway"]["status"] == "VALID"]
    positive = [(r, o) for r, o in pairs if r["expected_event_risk"] == "HIGH"]
    negative = [(r, o) for r, o in pairs if r["expected_event_risk"] == "LOW"]
    return {
        "version": "context-eval-v1",
        "scenarios": rows,
        "evaluations": len(pairs),
        "schema_compliance": len(valid) / len(pairs),
        "event_risk_recall": sum(o["gateway"]["output"]["event_risk"] == "HIGH" for r, o in positive) / len(positive)
        if positive
        else None,
        "false_alarm_rate": sum(o["gateway"]["output"]["event_risk"] == "HIGH" for r, o in negative) / len(negative)
        if negative
        else None,
        "quant_conflict_accuracy": sum(
            o["gateway"]["output"]["quant_conflict"] == r["expected_quant_conflict"] for r, o in pairs
        )
        / len(pairs),
        "consistent_scenario_fraction": sum(
            len({_hash(run["gateway"]["output"]) for run in row["runs"]}) == 1 for row in rows
        )
        / len(rows),
        "blocked_fraction": sum(o["candidate_effect"] == "BLOCK" for r, o in pairs) / len(pairs),
        "mean_latency_ms": sum(o["gateway"]["latency_ms"] for r, o in pairs) / len(pairs),
        "known_cost_usd": sum(
            a["cost_usd"] for r, o in pairs for a in o["gateway"]["attempts"] if a["cost_usd"] is not None
        ),
        "unknown_cost_attempts": sum(a["cost_usd"] is None for r, o in pairs for a in o["gateway"]["attempts"]),
        "semantic_hallucination_rate": None,
        "realized_trading_impact": None,
    }
