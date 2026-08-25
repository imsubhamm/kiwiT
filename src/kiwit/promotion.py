from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any

from .persistence import canonical_json

MANDATORY_EVIDENCE_GATES = (
    "point_in_time_universe",
    "minimum_200_trades",
    "cost_stress",
    "walk_forward_majority_positive",
    "random_entry_percentile",
    "parameter_neighborhood",
    "maximum_drawdown",
    "median_trade_after_costs",
)


@dataclass(frozen=True)
class StrategyApproval:
    approved_by: str
    reason: str
    approved_at: datetime

    def __post_init__(self) -> None:
        if not self.approved_by.strip() or not self.reason.strip():
            raise ValueError("strategy approval requires an approver and reason")
        if self.approved_at.tzinfo is None:
            raise ValueError("strategy approval timestamp must be timezone-aware")


@dataclass(frozen=True)
class PromotionEvidence:
    run_fingerprint: str
    report_sha256: str
    gates: Mapping[str, bool]

    def __post_init__(self) -> None:
        if len(self.run_fingerprint) != 64 or len(self.report_sha256) != 64:
            raise ValueError("promotion evidence requires SHA-256 fingerprints")
        frozen = MappingProxyType(dict(self.gates))
        object.__setattr__(self, "gates", frozen)

    @property
    def failed_gates(self) -> tuple[str, ...]:
        return tuple(name for name in MANDATORY_EVIDENCE_GATES if self.gates.get(name) is not True)

    def require_all_passed(self) -> None:
        if self.failed_gates:
            raise ValueError(f"strategy promotion blocked by evidence gates: {', '.join(self.failed_gates)}")


@dataclass(frozen=True)
class PromotedStrategy:
    strategy_id: str
    version: str
    specification: Mapping[str, Any]
    evidence: PromotionEvidence
    approval: StrategyApproval
    specification_sha256: str = ""

    def __post_init__(self) -> None:
        if not self.strategy_id.strip() or not self.version.strip():
            raise ValueError("strategy ID and version are required")
        self.evidence.require_all_passed()
        frozen_specification = MappingProxyType(dict(self.specification))
        object.__setattr__(self, "specification", frozen_specification)
        digest = hashlib.sha256(canonical_json(dict(frozen_specification)).encode()).hexdigest()
        if self.specification_sha256 and self.specification_sha256 != digest:
            raise ValueError("strategy specification fingerprint mismatch")
        object.__setattr__(self, "specification_sha256", digest)

    @property
    def key(self) -> tuple[str, str]:
        return self.strategy_id, self.version


class PromotedStrategyCatalog:
    """In-memory, immutable-version allowlist used by signal and workflow boundaries."""

    def __init__(self) -> None:
        self._records: dict[tuple[str, str], PromotedStrategy] = {}

    def register(self, record: PromotedStrategy) -> None:
        existing = self._records.get(record.key)
        if existing and existing != record:
            raise ValueError(f"promoted strategy version is immutable: {record.strategy_id}@{record.version}")
        self._records[record.key] = record

    def require_promoted(self, strategy_id: str, version: str) -> PromotedStrategy:
        try:
            return self._records[(strategy_id, version)]
        except KeyError as exc:
            raise PermissionError(f"strategy is not approved for paper trading: {strategy_id}@{version}") from exc

    def is_promoted(self, strategy_id: str, version: str) -> bool:
        return (strategy_id, version) in self._records


def approval_now(approved_by: str, reason: str) -> StrategyApproval:
    return StrategyApproval(approved_by, reason, datetime.now(UTC))
