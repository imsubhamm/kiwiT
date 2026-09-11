"""Version-bound research training, transactional registry and fail-closed scoring."""

from __future__ import annotations

import json
import math
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path

from kiwit.marketdata.features import FEATURE_VERSION, FeatureSnapshot

from .datasets import DATASET_VERSION, _hash, _json

FRAMEWORK_VERSION = "ml-framework-v1"


def _implementation(kind):
    from . import breakout, mean_reversion, regime, trend

    implementations = {
        "regime": (regime.RegimeConfig, regime.RegimeModel, regime.train_regime),
        "trend": (trend.TrendConfig, trend.TrendModel, trend.train_trend),
        "breakout": (breakout.BreakoutConfig, breakout.BreakoutModel, breakout.train_breakout),
        "mean_reversion": (
            mean_reversion.MeanReversionConfig,
            mean_reversion.MeanReversionModel,
            mean_reversion.train_mean_reversion,
        ),
    }
    if kind not in implementations:
        raise ValueError("Unknown model kind")
    return implementations[kind]


@dataclass(frozen=True)
class TrainingRun:
    kind: str
    dataset_fingerprint: str
    parameters: dict = field(default_factory=dict)
    feature_version: str = FEATURE_VERSION
    dataset_version: str = DATASET_VERSION
    framework_version: str = FRAMEWORK_VERSION
    calibration: str = "validation_temperature"
    model_version: str = ""

    def __post_init__(self):
        versions = {
            "regime": "regime-xgb-v1",
            "trend": "trend-xgb-v1",
            "breakout": "breakout-xgb-v1",
            "mean_reversion": "mean-reversion-xgb-v1",
        }
        if self.kind not in versions:
            raise ValueError("Unknown model kind")
        if not self.model_version:
            object.__setattr__(self, "model_version", versions[self.kind])
        if self.model_version != versions[self.kind]:
            raise ValueError("Unsupported model version")

    def train(self, dataset):
        if (
            self.feature_version != FEATURE_VERSION
            or self.dataset_version != DATASET_VERSION
            or self.framework_version != FRAMEWORK_VERSION
            or self.calibration != "validation_temperature"
        ):
            raise ValueError("Unsupported training contract or calibration hook")
        if (
            dataset["fingerprint"] != self.dataset_fingerprint
            or dataset["feature_version"] != self.feature_version
            or dataset["dataset_version"] != self.dataset_version
        ):
            raise ValueError("Training dataset binding mismatch")
        config, _, train = _implementation(self.kind)
        return train(dataset, config=config(**self.parameters))


class ModelRegistry:
    """Immutable artifacts and per-kind research activation history in one SQLite store."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS artifacts (
                    fingerprint TEXT PRIMARY KEY, kind TEXT NOT NULL,
                    body TEXT NOT NULL, run TEXT NOT NULL, run_fingerprint TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS activations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL,
                    fingerprint TEXT NOT NULL REFERENCES artifacts(fingerprint));
                CREATE TABLE IF NOT EXISTS active (
                    kind TEXT PRIMARY KEY, activation_id INTEGER NOT NULL REFERENCES activations(id));
            """)

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.path)
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def train_and_register(self, run: TrainingRun, dataset: dict) -> str:
        model = run.train(dataset)
        artifact = model.artifact
        _, cls, _ = _implementation(run.kind)
        cls(artifact)  # Validate native model and checksum before persisting.
        body, run_body = _json(artifact), _json(asdict(run))
        fingerprint = artifact["fingerprint"]
        with self._connect() as db:
            existing = db.execute("SELECT kind, body FROM artifacts WHERE fingerprint=?", (fingerprint,)).fetchone()
            if existing and existing != (run.kind, body):
                raise ValueError("Immutable artifact conflict")
            db.execute(
                "INSERT OR IGNORE INTO artifacts VALUES (?, ?, ?, ?, ?)",
                (fingerprint, run.kind, body, run_body, _hash(asdict(run))),
            )
        return fingerprint

    def _load(self, db, kind, fingerprint):
        row = db.execute(
            "SELECT kind, body, run, run_fingerprint FROM artifacts WHERE fingerprint=?", (fingerprint,)
        ).fetchone()
        if row is None or row[0] != kind:
            raise ValueError("Model not registered for requested kind")
        artifact, run_data = json.loads(row[1]), json.loads(row[2])
        run = TrainingRun(**run_data)
        if (
            _hash(run_data) != row[3]
            or run.kind != kind
            or artifact["fingerprint"] != fingerprint
            or run.model_version != artifact["model_version"]
            or run.dataset_fingerprint != artifact["dataset_fingerprint"]
            or run.feature_version != artifact["feature_version"]
            or run.dataset_version != artifact["dataset_version"]
            or run.framework_version != FRAMEWORK_VERSION
            or run.calibration != "validation_temperature"
        ):
            raise ValueError("Registry version binding mismatch")
        config, cls, _ = _implementation(kind)
        if asdict(config(**run.parameters)) != artifact["config"]:
            raise ValueError("Training config binding mismatch")
        return cls(artifact)

    def activate(self, kind: str, fingerprint: str) -> int:
        """Activate only for this local research registry; does not deploy a service."""
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._load(db, kind, fingerprint)
            cursor = db.execute("INSERT INTO activations(kind, fingerprint) VALUES (?, ?)", (kind, fingerprint))
            db.execute(
                "INSERT INTO active VALUES (?, ?) ON CONFLICT(kind) DO UPDATE SET activation_id=excluded.activation_id",
                (kind, cursor.lastrowid),
            )
            return cursor.lastrowid

    def rollback(self, kind: str) -> str:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute("SELECT activation_id FROM active WHERE kind=?", (kind,)).fetchone()
            if current is None:
                raise ValueError("No active model to roll back")
            previous = db.execute(
                "SELECT id, fingerprint FROM activations WHERE kind=? AND id<? ORDER BY id DESC LIMIT 1",
                (kind, current[0]),
            ).fetchone()
            if previous is None:
                raise ValueError("No previous activation")
            self._load(db, kind, previous[1])
            db.execute("UPDATE active SET activation_id=? WHERE kind=?", (previous[0], kind))
            return previous[1]

    def score(self, kind: str, snapshot: FeatureSnapshot) -> dict:
        result = {
            "framework_version": FRAMEWORK_VERSION,
            "kind": kind,
            "status": "BLOCKED",
            "direction": "NONE",
            "prediction": None,
            "model_fingerprint": None,
            "model_version": None,
            "feature_version": snapshot.version,
            "snapshot_fingerprint": _hash(snapshot.to_json_dict()),
            "dataset_fingerprint": None,
            "activation_id": None,
        }
        try:
            with self._connect() as db:
                row = db.execute(
                    "SELECT a.id, a.fingerprint FROM active p JOIN activations a ON a.id=p.activation_id WHERE p.kind=? AND a.kind=p.kind",
                    (kind,),
                ).fetchone()
                if row is None:
                    return {**result, "reason_codes": ["NO_ACTIVE_MODEL"]}
                result.update(activation_id=row[0], model_fingerprint=row[1])
                model = self._load(db, kind, row[1])
            artifact = model.artifact
            result.update(model_version=artifact["model_version"], dataset_fingerprint=artifact["dataset_fingerprint"])
            if snapshot.version != artifact["feature_version"] or not snapshot.ready:
                return {**result, "reason_codes": ["FEATURES_INCOMPATIBLE_OR_UNREADY"]}
            if snapshot.calendar_version != artifact["calendar_version"]:
                return {**result, "reason_codes": ["CALENDAR_VERSION_MISMATCH"]}
            prediction = model.predict(snapshot)
            _json(prediction)  # Reject nonfinite probabilities and non-JSON output.
            if kind == "regime":
                probabilities = list(prediction.get("scores", {}).values())
            else:
                key = {
                    "trend": "opportunity_probability",
                    "breakout": "continuation_probability",
                    "mean_reversion": "reversion_probability",
                }[kind]
                probabilities = [prediction.get(key)]
            if not probabilities or any(p is None or not math.isfinite(p) or not 0 <= p <= 1 for p in probabilities):
                return {**result, "reason_codes": prediction.get("reason_codes", ["INVALID_SCORE"])}
            return {
                **result,
                "status": "SCORED",
                "direction": prediction.get("direction", "NONE"),
                "prediction": prediction,
                "reason_codes": prediction["reason_codes"],
            }
        except (ValueError, KeyError, TypeError, RuntimeError, sqlite3.Error, ImportError, OSError):
            return {**result, "reason_codes": ["MODEL_LOAD_OR_SCORE_FAILED"]}
