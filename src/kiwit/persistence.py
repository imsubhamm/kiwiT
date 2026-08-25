from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Mapping
from uuid import UUID, uuid4


LOCAL_SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS datasets (
    dataset_id TEXT PRIMARY KEY, name TEXT NOT NULL, source_type TEXT NOT NULL,
    source_uri TEXT NOT NULL, content_sha256 TEXT NOT NULL UNIQUE,
    period_start TEXT, period_end TEXT, row_count INTEGER NOT NULL,
    retrieved_at TEXT NOT NULL, metadata_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS strategy_versions (
    strategy_id TEXT NOT NULL, version TEXT NOT NULL, status TEXT NOT NULL,
    specification_json TEXT NOT NULL, specification_sha256 TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL, PRIMARY KEY(strategy_id, version)
);
CREATE TABLE IF NOT EXISTS backtest_runs (
    run_id TEXT PRIMARY KEY, run_fingerprint TEXT NOT NULL UNIQUE,
    strategy_id TEXT NOT NULL, strategy_version TEXT NOT NULL,
    dataset_ids_json TEXT NOT NULL, code_sha256 TEXT NOT NULL,
    parameters_json TEXT NOT NULL, metrics_json TEXT NOT NULL,
    started_at TEXT NOT NULL, completed_at TEXT NOT NULL, created_at TEXT NOT NULL,
    FOREIGN KEY(strategy_id, strategy_version) REFERENCES strategy_versions(strategy_id, version)
);
"""


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


@dataclass(frozen=True)
class DatasetRecord:
    name: str
    source_type: str
    source_uri: str
    content_sha256: str
    row_count: int
    retrieved_at: datetime
    period_start: date | None = None
    period_end: date | None = None
    metadata: Mapping[str, Any] | None = None
    dataset_id: UUID = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.dataset_id is None:
            object.__setattr__(self, "dataset_id", uuid4())
        if len(self.content_sha256) != 64 or self.row_count < 0:
            raise ValueError("invalid dataset record")


@dataclass(frozen=True)
class BacktestRunRecord:
    run_id: UUID
    run_fingerprint: str
    strategy_id: str
    strategy_version: str
    dataset_ids: tuple[UUID, ...]
    code_sha256: str
    parameters: Mapping[str, Any]
    metrics: Mapping[str, Any]
    started_at: datetime
    completed_at: datetime


class LocalResearchStore:
    """SQLite adapter for local development with production-like immutability constraints."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path)
        self.connection.executescript(LOCAL_SCHEMA)

    def close(self) -> None:
        self.connection.close()

    def register_dataset(self, record: DatasetRecord) -> UUID:
        existing = self.connection.execute("SELECT dataset_id FROM datasets WHERE content_sha256=?", (record.content_sha256,)).fetchone()
        if existing:
            return UUID(existing[0])
        self.connection.execute(
            "INSERT INTO datasets VALUES (?,?,?,?,?,?,?,?,?,?)",
            (str(record.dataset_id), record.name, record.source_type, record.source_uri, record.content_sha256,
             record.period_start.isoformat() if record.period_start else None,
             record.period_end.isoformat() if record.period_end else None, record.row_count,
             record.retrieved_at.astimezone(UTC).isoformat(), canonical_json(record.metadata or {})),
        )
        self.connection.commit()
        return record.dataset_id

    def register_strategy(self, strategy_id: str, version: str, status: str, specification: Mapping[str, Any]) -> str:
        payload = canonical_json(specification)
        digest = hashlib.sha256(payload.encode()).hexdigest()
        existing = self.connection.execute(
            "SELECT specification_sha256 FROM strategy_versions WHERE strategy_id=? AND version=?",
            (strategy_id, version),
        ).fetchone()
        if existing:
            if existing[0] != digest:
                raise ValueError(f"strategy version is immutable: {strategy_id}@{version}")
            return digest
        self.connection.execute(
            "INSERT INTO strategy_versions VALUES (?,?,?,?,?,?)",
            (strategy_id, version, status, payload, digest, datetime.now(UTC).isoformat()),
        )
        self.connection.commit()
        return digest

    def record_backtest(
        self,
        strategy_id: str,
        strategy_version: str,
        dataset_ids: tuple[UUID, ...],
        code_sha256: str,
        parameters: Mapping[str, Any],
        metrics: Mapping[str, Any],
        started_at: datetime,
        completed_at: datetime,
    ) -> BacktestRunRecord:
        fingerprint_payload = {
            "strategy": f"{strategy_id}@{strategy_version}", "datasets": sorted(map(str, dataset_ids)),
            "code_sha256": code_sha256, "parameters": parameters,
        }
        fingerprint = hashlib.sha256(canonical_json(fingerprint_payload).encode()).hexdigest()
        existing = self.connection.execute("SELECT run_id FROM backtest_runs WHERE run_fingerprint=?", (fingerprint,)).fetchone()
        if existing:
            raise ValueError(f"immutable run already exists: {existing[0]}")
        record = BacktestRunRecord(uuid4(), fingerprint, strategy_id, strategy_version, dataset_ids, code_sha256, parameters, metrics, started_at, completed_at)
        self.connection.execute(
            "INSERT INTO backtest_runs VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (str(record.run_id), fingerprint, strategy_id, strategy_version, canonical_json(list(map(str, dataset_ids))),
             code_sha256, canonical_json(parameters), canonical_json(metrics), started_at.isoformat(), completed_at.isoformat(), datetime.now(UTC).isoformat()),
        )
        self.connection.commit()
        return record

    def list_backtests(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT run_id, run_fingerprint, strategy_id, strategy_version, parameters_json, metrics_json, completed_at FROM backtest_runs ORDER BY completed_at"
        ).fetchall()
        return [{"run_id": row[0], "run_fingerprint": row[1], "strategy_id": row[2], "strategy_version": row[3],
                 "parameters": json.loads(row[4]), "metrics": json.loads(row[5]), "completed_at": row[6]} for row in rows]
