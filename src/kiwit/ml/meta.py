"""Deterministic eligibility and ranking; never an execution or risk approval."""

from copy import deepcopy
from dataclasses import asdict, dataclass, field
from typing import Protocol

from .datasets import _hash
from .uncertainty import UncertaintyConfig, candidates_for_risk, evaluate_and_record

VERSION = "meta-rules-v1"


class CandidateRanker(Protocol):
    version: str

    def rank(self, candidates: tuple[dict, ...]) -> list[str]: ...


@dataclass(frozen=True)
class MetaConfig:
    uncertainty: UncertaintyConfig = field(default_factory=UncertaintyConfig)
    strategy_priority: tuple[str, ...] = ("trend", "breakout", "mean_reversion")
    ranker_version: str = "confidence-priority-v1"

    def __post_init__(self):
        if len(self.strategy_priority) != 3 or set(self.strategy_priority) != {"trend", "breakout", "mean_reversion"}:
            raise ValueError("Priority must contain each specialist exactly once")
        if not self.ranker_version:
            raise ValueError("Ranker version is required")


class MetaDecisionEngine:
    def __init__(self, config=None, *, ranker: CandidateRanker | None = None):
        self.config = config or MetaConfig()
        self.ranker = ranker
        version = ranker.version if ranker is not None else "confidence-priority-v1"
        if version != self.config.ranker_version:
            raise ValueError("Ranker version differs from configuration")

    def evaluate(self, snapshot, scores, *, now, checks, journal, market, previous=()):
        """Replay and paper callers supply the same clock/input contract and journal."""
        policy = evaluate_and_record(
            snapshot,
            scores,
            now=now,
            checks=checks,
            journal=journal,
            market=market,
            previous=previous,
            config=self.config.uncertainty,
        )
        candidates = candidates_for_risk(policy)
        reasons = list(policy["reason_codes"])
        ordered = []
        if candidates:
            if self.ranker is None:
                ordered = [
                    c["candidate_id"]
                    for c in sorted(
                        candidates,
                        key=lambda c: (
                            -c["confidence"],
                            self.config.strategy_priority.index(c["strategy"]),
                            c["candidate_id"],
                        ),
                    )
                ]
            else:
                try:
                    # A future supervised/RL ranker can reorder eligible candidates only.
                    ordered = self.ranker.rank(deepcopy(candidates))
                    ids = {c["candidate_id"] for c in candidates}
                    if (
                        not isinstance(ordered, list)
                        or any(type(i) is not str for i in ordered)
                        or len(set(ordered)) != len(ordered)
                        or set(ordered) != ids
                    ):
                        raise ValueError("Ranker must return a permutation of eligible candidate IDs")
                except Exception:  # noqa: BLE001 - extension failures must fail closed and be journaled
                    ordered = []
                    reasons = ["RANKER_FAILED_OR_INVALID"]
        chosen = next((c for c in candidates if ordered and c["candidate_id"] == ordered[0]), None)
        result = {
            "meta_version": VERSION,
            "config": asdict(self.config),
            "policy_decision_id": policy["decision_id"],
            "evaluated_at": policy["evaluated_at"],
            "snapshot_fingerprint": policy["snapshot_fingerprint"],
            "eligibility": "TRADE" if chosen else "NO_TRADE",
            "execution_allowed": False,
            "risk_approval_required": True,
            "selected_candidate": chosen,
            "ranking": ordered,
            "reason_codes": ["HIGHEST_RANKED_ELIGIBLE_CANDIDATE", "RISK_APPROVAL_REQUIRED"] if chosen else reasons,
            "explanation": "Confidence descending, configured strategy priority, then candidate ID"
            if self.ranker is None
            else "Versioned custom ranking of policy-eligible candidates only",
        }
        result["meta_decision_id"] = _hash(result)
        journal.append_event(
            policy["decision_id"],
            result["meta_decision_id"],
            at=now,
            kind="risk",
            details={"stage": "META_ELIGIBILITY_NOT_RISK_APPROVAL", "decision": result},
        )
        return result


def candidate_for_risk_review(result):
    """No truthiness fallback: unknown, changed and rejected states cannot proceed."""
    if (
        result.get("meta_version") != VERSION
        or result.get("eligibility") != "TRADE"
        or result.get("execution_allowed") is not False
        or result.get("risk_approval_required") is not True
    ):
        return None
    body = dict(result)
    fingerprint = body.pop("meta_decision_id", None)
    if fingerprint != _hash(body):
        return None
    return deepcopy(result.get("selected_candidate"))
