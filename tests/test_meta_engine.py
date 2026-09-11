from dataclasses import replace

import pytest
from test_uncertainty_policy import Journal, fixture

from kiwit.ml.meta import MetaConfig, MetaDecisionEngine, candidate_for_risk_review


class Audit(Journal):
    def append_event(self, *args, **kwargs):
        self.records.append((args, kwargs))


def run(engine=None, *, checks_override=None, journal=None):
    snap, scores, checks = fixture()
    checks.update(checks_override or {})
    return (engine or MetaDecisionEngine()).evaluate(
        snap, scores, now=snap.as_of, checks=checks, journal=journal or Audit(), market=[]
    )


def test_replay_and_paper_share_deterministic_result():
    replay = run()
    paper = run()
    assert replay == paper
    assert replay["eligibility"] == "TRADE"
    assert replay["selected_candidate"]["strategy"] == "trend"
    assert replay["execution_allowed"] is False
    assert replay["risk_approval_required"] is True
    assert candidate_for_risk_review(replay) == replay["selected_candidate"]
    assert candidate_for_risk_review({**replay, "execution_allowed": True}) is None


def test_uncertain_checks_no_trade_and_no_ranker_bypass():
    class Ranker:
        version = "fixture"

        def rank(self, candidates):
            pytest.fail("Ranker must not receive rejected candidates")

    engine = MetaDecisionEngine(replace(MetaConfig(), ranker_version="fixture"), ranker=Ranker())
    result = run(engine, checks_override={"critic": "UNKNOWN"})
    assert result["eligibility"] == "NO_TRADE"
    assert candidate_for_risk_review(result) is None


def test_extension_may_reorder_but_cannot_invent_candidates():
    class Ranker:
        version = "fixture"

        def rank(self, candidates):
            return [c["candidate_id"] for c in reversed(candidates)]

    config = replace(MetaConfig(), ranker_version="fixture")
    result = run(MetaDecisionEngine(config, ranker=Ranker()))
    assert result["selected_candidate"]["strategy"] == "breakout"

    class Invalid(Ranker):
        def rank(self, candidates):
            return ["invented"]

    result = run(MetaDecisionEngine(config, ranker=Invalid()))
    assert result["eligibility"] == "NO_TRADE"
    assert result["reason_codes"] == ["RANKER_FAILED_OR_INVALID"]
    with pytest.raises(ValueError):
        MetaDecisionEngine(ranker=Ranker())


def test_meta_audit_is_required_before_return():
    class FailedAudit(Audit):
        def append_event(self, *args, **kwargs):
            raise OSError("disk full")

    with pytest.raises(OSError):
        run(journal=FailedAudit())
    audit = Audit()
    result = run(journal=audit)
    assert len(audit.records) == 2
    assert audit.records[-1][1]["details"]["decision"] == result


def test_priority_breaks_equal_scores():
    snap, scores, checks = fixture()
    scores["breakout"]["prediction"]["continuation_probability"] = 0.8
    engine = MetaDecisionEngine(MetaConfig(strategy_priority=("breakout", "trend", "mean_reversion")))
    result = engine.evaluate(snap, scores, now=snap.as_of, checks=checks, journal=Audit(), market=[])
    assert result["selected_candidate"]["strategy"] == "breakout"
