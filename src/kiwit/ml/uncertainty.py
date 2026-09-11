"""Versioned fail-closed abstention policy; passing only permits further risk review."""

import math
from dataclasses import asdict, dataclass, field
from datetime import datetime

from kiwit.marketdata.canonical import utc
from kiwit.marketdata.features import FeatureSnapshot

from .candidates import CandidateConfig, generate_candidates
from .datasets import _hash

VERSION = "uncertainty-policy-v1"


@dataclass(frozen=True)
class UncertaintyConfig:
    candidates: CandidateConfig = field(default_factory=CandidateConfig)
    maximum_age_seconds: int = 90
    maximum_volatility: float = 0.01
    maximum_spread_fraction: float = 0.002
    minimum_depth: float = 1
    minimum_context_confidence: float = 0.7
    earliest_session_fraction: float = 0.02
    latest_session_fraction: float = 0.95

    def __post_init__(self):
        if type(self.maximum_age_seconds) is not int or self.maximum_age_seconds < 0:
            raise ValueError("Invalid maximum age")
        for value in (
            self.maximum_volatility,
            self.maximum_spread_fraction,
            self.minimum_context_confidence,
            self.earliest_session_fraction,
            self.latest_session_fraction,
        ):
            if not math.isfinite(value) or not 0 <= value <= 1:
                raise ValueError("Invalid fraction threshold")
        if not math.isfinite(self.minimum_depth) or self.minimum_depth <= 0:
            raise ValueError("Depth must be positive")
        if self.earliest_session_fraction >= self.latest_session_fraction:
            raise ValueError("Invalid session window")


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def evaluate_and_record(
    snapshot: FeatureSnapshot, scores: dict, *, now: datetime, checks: dict, journal, market, previous=(), config=None
):
    """Write evidence before returning. Logging failure raises and never returns permission."""
    config = config or UncertaintyConfig()
    now = utc(now)
    reasons = []
    age = (now - utc(snapshot.as_of)).total_seconds()
    if not 0 <= age <= config.maximum_age_seconds:
        reasons.append("STALE_OR_FUTURE_DATA")
    if not snapshot.ready:
        reasons.append("INSUFFICIENT_OR_INVALID_DATA")
    try:
        generated = generate_candidates(snapshot, scores, previous=previous, config=config.candidates)
    except (ValueError, TypeError, KeyError, AttributeError):
        generated = {
            "status": "NO_CANDIDATE",
            "candidates": [],
            "reason_codes": ["INVALID_MODEL_OR_COOLDOWN_INPUT"],
            "rejections": [],
            "conflicts": [],
        }
    if not generated["candidates"]:
        reasons.extend(generated["reason_codes"])
        reasons.extend(r["reason"] for r in generated["rejections"])
    # An unavailable specialist is uncertainty even if another specialist produced a candidate.
    if any(r["reason"] == "MODEL_UNAVAILABLE_OR_MISMATCH" for r in generated["rejections"]):
        reasons.append("INCOMPLETE_MODEL_EVIDENCE")
    volatility = snapshot.values.get("realized_volatility_20")
    if not _number(volatility) or volatility < 0:
        reasons.append("VOLATILITY_UNKNOWN")
    elif volatility > config.maximum_volatility:
        reasons.append("EXCESS_VOLATILITY")
    fraction = snapshot.values.get("session_fraction")
    if not _number(fraction) or not config.earliest_session_fraction <= fraction < config.latest_session_fraction:
        reasons.append("OUTSIDE_ENTRY_WINDOW")
    try:
        check_age = (now - utc(datetime.fromisoformat(checks["as_of"]))).total_seconds()
        fresh = 0 <= check_age <= config.maximum_age_seconds and checks["snapshot_fingerprint"] == _hash(
            snapshot.to_json_dict()
        )
    except (ValueError, TypeError, KeyError):
        fresh = False
    if not fresh:
        reasons.append("CHECK_EVIDENCE_STALE_OR_MISMATCH")
    spread, depth = checks.get("spread_fraction"), checks.get("depth")
    if not _number(spread) or spread < 0 or not _number(depth) or depth < 0:
        reasons.append("LIQUIDITY_UNKNOWN")
    elif spread > config.maximum_spread_fraction or depth < config.minimum_depth:
        reasons.append("INSUFFICIENT_LIQUIDITY")
    confidence = checks.get("context_confidence")
    if not _number(confidence) or not config.minimum_context_confidence <= confidence <= 1:
        reasons.append("CONTEXT_UNCERTAIN")
    if checks.get("critic") != "PASS":
        reasons.append("CRITIC_UNCERTAIN_OR_REJECTED")
    result = {
        "policy_version": VERSION,
        "config": asdict(config),
        "evaluated_at": now.isoformat(),
        "snapshot_fingerprint": _hash(snapshot.to_json_dict()),
        "action": "NO_TRADE" if reasons else "CONTINUE_TO_RISK",
        "execution_allowed": False,
        "reason_codes": sorted(set(reasons)) or ["RISK_REVIEW_REQUIRED"],
        "candidates": [] if reasons else generated["candidates"],
        "candidate_evidence": generated,
        "checks": checks,
    }
    result["decision_id"] = _hash(result)
    journal.record(
        result["decision_id"],
        snapshot,
        market,
        action="NO_TRADE" if reasons else "REVIEW",
        reason_codes=result["reason_codes"],
        models=[],
        candidate={"candidates": generated["candidates"]},
        risk_checks=result,
    )
    return result


def candidates_for_risk(decision):
    """Only the explicit positive policy state can proceed; absent/unknown states block."""
    if (
        decision.get("policy_version") != VERSION
        or decision.get("action") != "CONTINUE_TO_RISK"
        or decision.get("execution_allowed") is not False
        or decision.get("reason_codes") != ["RISK_REVIEW_REQUIRED"]
    ):
        return ()
    body = dict(decision)
    fingerprint = body.pop("decision_id", None)
    if fingerprint != _hash(body):
        return ()
    return tuple(decision["candidates"])
