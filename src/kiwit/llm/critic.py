"""Bound, advisory-only trade critique with outcome-blind benchmark inputs."""

import json
from copy import deepcopy
from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import Field, model_validator

from kiwit.marketdata.canonical import utc
from kiwit.ml.datasets import _hash

from .context import ContextRequest
from .gateway import ContextOutput, Strict

VERSION = "trade-critic-v1"


class Proposal(Strict):
    candidate_id: str = Field(min_length=1, max_length=128)
    direction: Literal["LONG", "SHORT"]
    entry: str
    stop: str
    target: str
    quantity: int = Field(gt=0)
    confidence: float = Field(ge=0.65, le=1)
    thesis: str = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def prices(self):
        entry, stop, target = (Decimal(v) for v in (self.entry, self.stop, self.target))
        if any(not v.is_finite() or v <= 0 for v in (entry, stop, target)):
            raise ValueError("Invalid proposal price")
        if not (stop < entry < target if self.direction == "LONG" else target < entry < stop):
            raise ValueError("Invalid directional price geometry")
        return self


class FeatureSummary(Strict):
    rsi: float = Field(ge=0, le=100)
    atr: float = Field(gt=0)
    ema_distance: float


class CriticRequest(ContextRequest):
    features: FeatureSummary
    proposal: Proposal
    context: ContextOutput
    historical_similarity: str = Field(min_length=1, max_length=2000)


class TradeCritic:
    def __init__(self, gateway):
        self.gateway = gateway

    def _record(self, result):
        self.gateway.audit.append("trade_critic", self.gateway.redactor.clean(result))
        return result

    async def critique(self, payload):
        try:
            request = CriticRequest.model_validate(payload)
            if not set(request.context.source_ids) <= {e.source_id for e in request.evidence}:
                raise ValueError("Unsupported context evidence")
            if (
                request.context.event_risk != "LOW"
                or request.context.quant_conflict != "NO"
                or request.context.market_context == "UNKNOWN"
                or request.context.confidence < 0.7
            ):
                raise ValueError("Context veto remains binding")
            proposal = request.proposal.model_dump()
            normalized = {
                "snapshot_fingerprint": request.snapshot_fingerprint,
                "candidate_id": request.proposal.candidate_id,
                "direction": request.proposal.direction,
                "thesis": request.proposal.thesis,
                "candidate_confidence": request.proposal.confidence,
                "quantitative_summary": json.dumps(
                    {
                        "instrument": request.instrument,
                        "as_of": request.as_of,
                        "proposal": proposal,
                        "quant": request.quant.model_dump(),
                        "features": request.features.model_dump(),
                        "context": request.context.model_dump(),
                        "historical_similarity": request.historical_similarity,
                    },
                    sort_keys=True,
                ),
                "sources": [
                    {"source_id": e.source_id, "text": json.dumps(e.model_dump(), sort_keys=True)}
                    for e in request.evidence
                ],
            }
        except (ValueError, TypeError, ArithmeticError):
            response = await self.gateway.critique_trade({})
            return self._record(
                {
                    "critic_version": VERSION,
                    "status": "NO_TRADE",
                    "execution_allowed": False,
                    "proposal": None,
                    "proposal_fingerprint": None,
                    "gateway": response,
                    "reason_codes": ["INVALID_OR_MISSING_CRITIC_EVIDENCE"],
                }
            )
        response = await self.gateway.critique_trade(normalized)
        output = response["output"]
        approved = (
            response["status"] == "VALID"
            and output["verdict"] == "APPROVE"
            and output["confidence"] >= 0.7
            and not output["warnings"]
        )
        result = {
            "critic_version": VERSION,
            "status": "RISK_REVIEW_REQUIRED" if approved else "NO_TRADE",
            "execution_allowed": False,
            "proposal": proposal,
            "proposal_fingerprint": _hash(proposal),
            "snapshot_fingerprint": request.snapshot_fingerprint,
            "as_of": request.as_of,
            "gateway": response,
            "reason_codes": ["CRITIC_APPROVED_RISK_STILL_REQUIRED"] if approved else ["CRITIC_REJECTED_OR_UNCERTAIN"],
        }
        result["review_fingerprint"] = _hash(result)
        return self._record(result)


def proposal_for_risk(result, proposal, *, snapshot_fingerprint):
    """Return only the caller's unchanged authoritative proposal after explicit approval."""
    body = dict(result)
    fingerprint = body.pop("review_fingerprint", None)
    if (
        fingerprint != _hash(body)
        or result.get("critic_version") != VERSION
        or result.get("status") != "RISK_REVIEW_REQUIRED"
        or result.get("execution_allowed") is not False
        or result.get("snapshot_fingerprint") != snapshot_fingerprint
        or result.get("proposal_fingerprint") != _hash(proposal)
    ):
        return None
    return deepcopy(proposal)


async def benchmark_critic(critic, scenarios, *, repeats=2):
    """Net endpoint returns are supplied separately and never included in model input."""
    if type(repeats) is not int or not 1 <= repeats <= 10:
        raise ValueError("Invalid repeat count")
    rows = []
    for item in sorted(scenarios, key=lambda s: utc(datetime.fromisoformat(s["input"]["as_of"]))):
        if set(item) != {"input", "outcome"} or set(item["outcome"]) != {"net_return", "exit_at"}:
            raise ValueError("Invalid frozen scenario")
        at = utc(datetime.fromisoformat(item["input"]["as_of"]))
        end = utc(datetime.fromisoformat(item["outcome"]["exit_at"]))
        net = Decimal(str(item["outcome"]["net_return"]))
        if end <= at or not net.is_finite() or net <= -1:
            raise ValueError("Invalid outcome")
        reviews = [await critic.critique(deepcopy(item["input"])) for _ in range(repeats)]
        review = reviews[0]
        rows.append(
            {
                "scenario_fingerprint": _hash(item),
                "at": at.isoformat(),
                "exit_at": end.isoformat(),
                "net_return": float(net),
                "review": review,
                "repeated_reviews": reviews,
            }
        )
    selected, next_entry = [], None
    for row in rows:
        if row["review"]["status"] != "RISK_REVIEW_REQUIRED":
            continue
        if next_entry is None or row["at"] >= next_entry:
            selected.append(row["net_return"])
            next_entry = row["exit_at"]
    equity = peak = 1.0
    drawdown = 0.0
    for value in selected:
        equity *= 1 + value
        peak = max(peak, equity)
        drawdown = max(drawdown, 1 - equity / peak)
    gains, losses = sum(max(0, v) for v in selected), -sum(min(0, v) for v in selected)
    rejected = [r for r in rows if r["review"]["status"] == "NO_TRADE"]
    return {
        "version": "critic-benchmark-v1",
        "rows": rows,
        "selected_trades": len(selected),
        "net_expectancy": sum(selected) / len(selected) if selected else None,
        "profit_factor": gains / losses if losses else None,
        "unit_equity_max_drawdown": drawdown,
        "losing_trades_rejected": sum(r["net_return"] < 0 for r in rejected),
        "winning_trades_falsely_rejected": sum(r["net_return"] > 0 for r in rejected),
        "no_trade_loss_fraction": sum(r["net_return"] < 0 for r in rejected) / len(rejected) if rejected else None,
        "schema_failure_count": sum(r["review"]["gateway"]["status"] != "VALID" for r in rows),
        "mean_latency_ms": sum(r["review"]["gateway"]["latency_ms"] for r in rows) / len(rows) if rows else None,
        "consistent_scenario_fraction": sum(
            len({_hash(v["gateway"]["output"]) for v in r["repeated_reviews"]}) == 1 for r in rows
        )
        / len(rows)
        if rows
        else None,
        "known_cost_usd": sum(
            a["cost_usd"]
            for r in rows
            for v in r["repeated_reviews"]
            for a in v["gateway"]["attempts"]
            if a["cost_usd"] is not None
        ),
        "unknown_cost_attempts": sum(
            a["cost_usd"] is None for r in rows for v in r["repeated_reviews"] for a in v["gateway"]["attempts"]
        ),
        "semantic_hallucination_rate": None,
    }
