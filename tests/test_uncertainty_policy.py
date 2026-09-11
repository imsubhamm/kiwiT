from dataclasses import replace
from datetime import timedelta
from types import MappingProxyType

import pytest
from test_candidates import inputs
from test_decision_journal import setup

from kiwit.ml.datasets import _hash
from kiwit.ml.uncertainty import UncertaintyConfig, candidates_for_risk, evaluate_and_record


class Journal:
    def __init__(self):
        self.records = []

    def record(self, *args, **kwargs):
        self.records.append((args, kwargs))


def fixture():
    snap, scores = inputs()
    snap = replace(
        snap, values=MappingProxyType({**snap.values, "realized_volatility_20": 0.001, "session_fraction": 0.5})
    )
    for score in scores.values():
        score["snapshot_fingerprint"] = _hash(snap.to_json_dict())
    checks = {
        "as_of": snap.as_of.isoformat(),
        "snapshot_fingerprint": _hash(snap.to_json_dict()),
        "spread_fraction": 0.001,
        "depth": 100,
        "context_confidence": 0.9,
        "critic": "PASS",
    }
    return snap, scores, checks


def test_positive_only_proceeds_to_risk_and_is_logged():
    snap, scores, checks = fixture()
    journal = Journal()
    result = evaluate_and_record(snap, scores, now=snap.as_of, checks=checks, journal=journal, market=[])
    assert result["action"] == "CONTINUE_TO_RISK"
    assert result["execution_allowed"] is False
    assert len(candidates_for_risk(result)) == 2
    assert journal.records[0][1]["action"] == "REVIEW"
    assert candidates_for_risk({**result, "action": "TRADE"}) == ()
    assert candidates_for_risk({**result, "candidates": []}) == ()


@pytest.mark.parametrize(
    "field,value,reason",
    [
        ("critic", None, "CRITIC_UNCERTAIN_OR_REJECTED"),
        ("context_confidence", 0.2, "CONTEXT_UNCERTAIN"),
        ("depth", None, "LIQUIDITY_UNKNOWN"),
        ("spread_fraction", 0.01, "INSUFFICIENT_LIQUIDITY"),
        ("snapshot_fingerprint", "wrong", "CHECK_EVIDENCE_STALE_OR_MISMATCH"),
    ],
)
def test_uncertainty_cannot_fall_through(field, value, reason):
    snap, scores, checks = fixture()
    checks[field] = value
    journal = Journal()
    result = evaluate_and_record(snap, scores, now=snap.as_of, checks=checks, journal=journal, market=[])
    assert result["action"] == "NO_TRADE"
    assert reason in result["reason_codes"]
    assert candidates_for_risk(result) == ()
    assert journal.records[0][1]["action"] == "NO_TRADE"


def test_stale_conflict_and_missing_models():
    snap, scores, checks = fixture()
    result = evaluate_and_record(
        snap, scores, now=snap.as_of + timedelta(minutes=2), checks=checks, journal=Journal(), market=[]
    )
    assert "STALE_OR_FUTURE_DATA" in result["reason_codes"]
    scores["mean_reversion"]["prediction"].update(direction="SHORT", reversion_probability=0.9)
    result = evaluate_and_record(snap, scores, now=snap.as_of, checks=checks, journal=Journal(), market=[])
    assert "DIRECTION_CONFLICT" in result["reason_codes"]
    scores.pop("mean_reversion")
    result = evaluate_and_record(snap, scores, now=snap.as_of, checks=checks, journal=Journal(), market=[])
    assert "INCOMPLETE_MODEL_EVIDENCE" in result["reason_codes"]


def test_journal_failure_blocks_return():
    class FailedJournal:
        def record(self, *args, **kwargs):
            raise OSError("disk unavailable")

    snap, scores, checks = fixture()
    with pytest.raises(OSError):
        evaluate_and_record(snap, scores, now=snap.as_of, checks=checks, journal=FailedJournal(), market=[])


def test_real_journal_retains_rejected_state(tmp_path):
    journal, _, market, snap = setup(tmp_path)
    result = evaluate_and_record(snap, {}, now=snap.as_of, checks={}, journal=journal, market=market)
    assert result["action"] == "NO_TRADE"
    with journal._connect() as db:
        row = db.execute(
            "SELECT payload,digest FROM decisions WHERE decision_id=?", (result["decision_id"],)
        ).fetchone()
    saved = journal._verified(row)
    assert saved["action"] == "NO_TRADE"
    assert saved["risk_checks"]["policy_version"] == result["policy_version"]
    with pytest.raises(ValueError):
        UncertaintyConfig(latest_session_fraction=0.01)


@pytest.mark.parametrize(
    "feature,value,reason",
    [
        ("realized_volatility_20", 0.02, "EXCESS_VOLATILITY"),
        ("realized_volatility_20", None, "VOLATILITY_UNKNOWN"),
        ("session_fraction", 0.95, "OUTSIDE_ENTRY_WINDOW"),
    ],
)
def test_market_restrictions(feature, value, reason):
    snap, scores, checks = fixture()
    snap = replace(snap, values=MappingProxyType({**snap.values, feature: value}))
    result = evaluate_and_record(snap, scores, now=snap.as_of, checks=checks, journal=Journal(), market=[])
    assert reason in result["reason_codes"]
    assert not candidates_for_risk(result)


def test_cooldown_is_no_trade():
    snap, scores, checks = fixture()
    first = evaluate_and_record(snap, scores, now=snap.as_of, checks=checks, journal=Journal(), market=[])
    second = evaluate_and_record(
        snap, scores, now=snap.as_of, checks=checks, journal=Journal(), market=[], previous=first["candidates"]
    )
    assert "DUPLICATE_OR_COOLDOWN" in second["reason_codes"]
    assert second["action"] == "NO_TRADE"
