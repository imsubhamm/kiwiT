from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Any, Mapping


class Severity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


@dataclass(frozen=True)
class NormalizedBar:
    trading_date: date
    symbol: str
    series: str
    open: float
    high: float
    low: float
    close: float
    volume: int | None
    source_sha256: str
    source_format: str
    isin: str | None = None


@dataclass(frozen=True)
class ManifestEntry:
    source_name: str
    source_uri: str
    local_path: str
    trading_date: date
    retrieved_at: datetime
    sha256: str
    byte_size: int
    status: str
    metadata: Mapping[str, Any]

    def to_json_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["trading_date"] = self.trading_date.isoformat()
        value["retrieved_at"] = self.retrieved_at.astimezone(UTC).isoformat()
        return value


@dataclass(frozen=True)
class ValidationIssue:
    severity: Severity
    code: str
    message: str
    trading_date: date | None = None


@dataclass(frozen=True)
class ValidationReport:
    dataset_name: str
    row_count: int
    issues: tuple[ValidationIssue, ...]

    @property
    def publishable(self) -> bool:
        return not any(issue.severity == Severity.ERROR for issue in self.issues)

