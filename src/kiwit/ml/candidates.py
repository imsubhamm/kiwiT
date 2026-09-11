"""Pure candidate generation: explicit evidence, conflicts and caller-owned cooldown history."""

import math
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta

from kiwit.marketdata.features import FEATURE_VERSION, FeatureSnapshot

from .datasets import _hash

VERSION = "strategy-candidates-v1"
COMPATIBILITY = {
    "UPTREND": {"trend", "breakout"},
    "DOWNTREND": {"trend", "breakout"},
    "RANGE": {"mean_reversion", "breakout"},
    "HIGH_VOLATILITY": set(),
}
PROBABILITIES = {
    "trend": "opportunity_probability",
    "breakout": "continuation_probability",
    "mean_reversion": "reversion_probability",
}


@dataclass(frozen=True)
class CandidateConfig:
    minimum_confidence: float = 0.65
    regime_confidence: float = 0.6
    cooldown_seconds: int = 300

    def __post_init__(self):
        if any(not math.isfinite(v) or not 0 <= v <= 1 for v in (self.minimum_confidence, self.regime_confidence)):
            raise ValueError("Confidence must be a finite probability")
        if type(self.cooldown_seconds) is not int or self.cooldown_seconds < 0:
            raise ValueError("Cooldown must be nonnegative integer seconds")


def generate_candidates(snapshot: FeatureSnapshot, scores: dict, *, previous=(), config=None):
    """Identical snapshot, scores, config and prior emitted candidates produce identical output."""
    config = config or CandidateConfig()
    digest = _hash(snapshot.to_json_dict())
    result = {
        "version": VERSION,
        "status": "NO_CANDIDATE",
        "candidates": [],
        "conflicts": [],
        "rejections": [],
        "snapshot_fingerprint": digest,
        "config": asdict(config),
        "reason_codes": [],
        "evidence": scores,
    }
    if not snapshot.ready or snapshot.version != FEATURE_VERSION:
        return {**result, "reason_codes": ["FEATURES_UNAVAILABLE"]}

    def valid(kind):
        score = scores.get(kind, {})
        prediction = score.get("prediction") or {}
        return (
            score.get("status") == "SCORED"
            and score.get("kind") == kind
            and score.get("snapshot_fingerprint") == digest
            and score.get("feature_version") == snapshot.version
            and bool(score.get("model_fingerprint"))
            and prediction.get("model_fingerprint") == score.get("model_fingerprint")
            and prediction.get("model_version") == score.get("model_version")
            and prediction.get("feature_version") == snapshot.version
        )

    if not valid("regime"):
        return {**result, "reason_codes": ["REGIME_UNAVAILABLE"]}
    regime = scores["regime"]["prediction"]
    name = regime.get("regime")
    probability = regime.get("scores", {}).get(name)
    if (
        name not in COMPATIBILITY
        or not isinstance(probability, (int, float))
        or not math.isfinite(probability)
        or not config.regime_confidence <= probability <= 1
    ):
        return {**result, "reason_codes": ["REGIME_UNCERTAIN"]}
    eligible = []
    for kind, key in PROBABILITIES.items():
        rejection = None
        if not valid(kind):
            rejection = "MODEL_UNAVAILABLE_OR_MISMATCH"
        else:
            prediction = scores[kind]["prediction"]
            direction, p = prediction.get("direction"), prediction.get(key)
            if (
                direction not in {"LONG", "SHORT"}
                or not isinstance(p, (int, float))
                or not math.isfinite(p)
                or not config.minimum_confidence <= p <= 1
            ):
                rejection = "WEAK_OR_UNCERTAIN"
            else:
                eligible.append((kind, direction, p))
        if rejection:
            result["rejections"].append({"strategy": kind, "reason": rejection})
    # Detect disagreement before regime filtering, so incompatible opposing evidence remains visible.
    if len({direction for _, direction, _ in eligible}) > 1:
        result["conflicts"] = [{"strategy": k, "direction": d, "confidence": p} for k, d, p in eligible]
        return {**result, "reason_codes": ["DIRECTION_CONFLICT"]}
    for kind, direction, p in eligible:
        if (
            kind not in COMPATIBILITY[name]
            or (name == "UPTREND" and direction != "LONG")
            or (name == "DOWNTREND" and direction != "SHORT")
        ):
            result["rejections"].append({"strategy": kind, "reason": "REGIME_INCOMPATIBLE"})
            continue
        body = {
            "version": VERSION,
            "instrument": snapshot.instrument.key,
            "at": snapshot.as_of.isoformat(),
            "strategy": kind,
            "direction": direction,
            "confidence": p,
            "regime": name,
            "snapshot_fingerprint": digest,
            "config": asdict(config),
            "model_fingerprint": scores[kind]["model_fingerprint"],
            "regime_fingerprint": scores["regime"]["model_fingerprint"],
            "thesis": f"{kind} {direction} opportunity compatible with {name}",
            "reason_codes": ["CONFIDENT_SPECIALIST", "REGIME_COMPATIBLE"],
        }
        candidate = {**body, "candidate_id": _hash(body)}
        suppressed = False
        for old in previous:
            if old["instrument"] != body["instrument"]:
                continue
            at = datetime.fromisoformat(old["at"])
            if at.tzinfo is None or at > snapshot.as_of:
                raise ValueError("Cooldown history must be timezone-aware and no later than decision")
            if old["candidate_id"] == candidate["candidate_id"] or (
                old["strategy"] == kind
                and old["direction"] == direction
                and snapshot.as_of - at < timedelta(seconds=config.cooldown_seconds)
            ):
                suppressed = True
        if suppressed:
            result["rejections"].append({"strategy": kind, "reason": "DUPLICATE_OR_COOLDOWN"})
        else:
            result["candidates"].append(candidate)
    result["status"] = "CANDIDATES" if result["candidates"] else "NO_CANDIDATE"
    result["reason_codes"] = ["CANDIDATES_GENERATED" if result["candidates"] else "NO_ELIGIBLE_CANDIDATE"]
    return result
