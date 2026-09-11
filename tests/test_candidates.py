from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import MappingProxyType

import pytest

from kiwit.marketdata.canonical import Instrument
from kiwit.marketdata.features import FEATURE_VERSION, FeatureSnapshot
from kiwit.ml.candidates import CandidateConfig, generate_candidates
from kiwit.ml.datasets import _hash


def inputs():
    snap = FeatureSnapshot(
        datetime(2026, 9, 1, 5, tzinfo=UTC),
        Instrument("TEST"),
        FEATURE_VERSION,
        "calendar",
        "input",
        MappingProxyType({"x": 1}),
        MappingProxyType({}),
        (),
        ("x",),
    )
    scores = {}
    for kind, pred in {
        "regime": {"regime": "UPTREND", "scores": {"UPTREND": 0.8}},
        "trend": {"direction": "LONG", "opportunity_probability": 0.8},
        "breakout": {"direction": "LONG", "continuation_probability": 0.75},
        "mean_reversion": {"direction": "NONE", "reversion_probability": 0.3},
    }.items():
        scores[kind] = {
            "kind": kind,
            "status": "SCORED",
            "snapshot_fingerprint": _hash(snap.to_json_dict()),
            "model_fingerprint": kind,
            "model_version": "v1",
            "feature_version": FEATURE_VERSION,
            "prediction": {
                **pred,
                "model_fingerprint": kind,
                "model_version": "v1",
                "feature_version": FEATURE_VERSION,
            },
        }
    return snap, scores


def test_deterministic_candidates_and_conflicts():
    snap, scores = inputs()
    result = generate_candidates(snap, scores)
    assert result == generate_candidates(snap, dict(reversed(list(scores.items()))))
    assert [c["strategy"] for c in result["candidates"]] == ["trend", "breakout"]
    scores["mean_reversion"]["prediction"].update(direction="SHORT", reversion_probability=0.9)
    conflict = generate_candidates(snap, scores)
    assert conflict["status"] == "NO_CANDIDATE"
    assert len(conflict["conflicts"]) == 3  # Incompatible opposition is retained.


def test_dedup_cooldown_and_expiry():
    snap, scores = inputs()
    old = generate_candidates(snap, scores)["candidates"]
    assert not generate_candidates(snap, scores, previous=old)["candidates"]
    for seconds, count in ((299, 0), (300, 2)):
        later = replace(snap, as_of=snap.as_of + timedelta(seconds=seconds))
        for score in scores.values():
            score["snapshot_fingerprint"] = _hash(later.to_json_dict())
        assert len(generate_candidates(later, scores, previous=old)["candidates"]) == count
    bad = [{**old[0], "at": (snap.as_of + timedelta(seconds=1)).isoformat()}]
    snap, scores = inputs()
    with pytest.raises(ValueError):
        generate_candidates(snap, scores, previous=bad)


def test_fail_closed_and_regime_compatibility():
    snap, scores = inputs()
    assert not generate_candidates(replace(snap, reasons=("STALE",)), scores)["candidates"]
    scores["regime"]["snapshot_fingerprint"] = "old"
    assert generate_candidates(snap, scores)["reason_codes"] == ["REGIME_UNAVAILABLE"]
    snap, scores = inputs()
    scores["regime"]["prediction"] = {
        **scores["regime"]["prediction"],
        "regime": "HIGH_VOLATILITY",
        "scores": {"HIGH_VOLATILITY": 0.9},
    }
    assert not generate_candidates(snap, scores)["candidates"]
    snap, scores = inputs()
    for kind in ("trend", "breakout"):
        scores[kind]["prediction"]["direction"] = "NONE"
    assert not generate_candidates(snap, scores)["candidates"]
    with pytest.raises(ValueError):
        CandidateConfig(minimum_confidence=float("nan"))
