# ruff: noqa: F811
import json
import sqlite3
from dataclasses import replace

import pytest

pytest.importorskip("xgboost")

from test_trend_model import dataset, snapshot  # noqa: F401

from kiwit.ml.framework import ModelRegistry, TrainingRun


def test_reproducible_training_and_exact_score_binding(dataset, tmp_path):
    registry = ModelRegistry(tmp_path / "models.sqlite3")
    run = TrainingRun("trend", dataset["fingerprint"], {"rounds": 2})
    fingerprint = registry.train_and_register(run, dataset)
    assert registry.train_and_register(run, dataset) == fingerprint
    activation = registry.activate("trend", fingerprint)
    score = registry.score("trend", snapshot(dataset))
    assert score["status"] == "SCORED"
    assert score["activation_id"] == activation
    assert score["model_fingerprint"] == fingerprint
    assert score["model_version"] == run.model_version
    assert score["feature_version"] == run.feature_version
    assert score["dataset_fingerprint"] == dataset["fingerprint"]
    assert score["prediction"]["model_fingerprint"] == fingerprint


def test_independent_rollback(dataset, tmp_path):
    registry = ModelRegistry(tmp_path / "models.sqlite3")
    first = registry.train_and_register(TrainingRun("trend", dataset["fingerprint"], {"rounds": 2}), dataset)
    second = registry.train_and_register(TrainingRun("trend", dataset["fingerprint"], {"rounds": 3}), dataset)
    other = registry.train_and_register(TrainingRun("breakout", dataset["fingerprint"], {"rounds": 2}), dataset)
    registry.activate("trend", first)
    registry.activate("trend", second)
    registry.activate("breakout", other)
    assert registry.rollback("trend") == first
    assert registry.score("trend", snapshot(dataset))["model_fingerprint"] == first
    assert registry.score("breakout", snapshot(dataset))["model_fingerprint"] == other
    with pytest.raises(ValueError, match="previous"):
        registry.rollback("trend")
    with pytest.raises(ValueError):
        registry.activate("trend", other)


def test_fail_closed_missing_corrupt_and_incompatible(dataset, tmp_path):
    registry = ModelRegistry(tmp_path / "models.sqlite3")
    snap = snapshot(dataset)
    assert registry.score("trend", snap)["status"] == "BLOCKED"
    key = registry.train_and_register(TrainingRun("trend", dataset["fingerprint"], {"rounds": 2}), dataset)
    registry.activate("trend", key)
    for bad in (
        replace(snap, version="old"),
        replace(snap, reasons=("STALE",)),
        replace(snap, calendar_version="other"),
    ):
        assert registry.score("trend", bad)["status"] == "BLOCKED"
    with sqlite3.connect(registry.path) as db:
        db.execute("UPDATE artifacts SET body='{}'")
    assert registry.score("trend", snap)["status"] == "BLOCKED"
    with pytest.raises(KeyError):
        registry.activate("trend", key)


def test_run_binding_and_calibration_hook(dataset, tmp_path):
    registry = ModelRegistry(tmp_path / "models.sqlite3")
    for run in (
        TrainingRun("trend", "wrong"),
        TrainingRun("trend", dataset["fingerprint"], calibration="test_temperature"),
    ):
        with pytest.raises(ValueError):
            registry.train_and_register(run, dataset)
    with pytest.raises(ValueError):
        TrainingRun("trend", dataset["fingerprint"], model_version="old")
    key = registry.train_and_register(TrainingRun("trend", dataset["fingerprint"], {"rounds": 2}), dataset)
    registry.activate("trend", key)
    with sqlite3.connect(registry.path) as db:
        run = json.loads(db.execute("SELECT run FROM artifacts").fetchone()[0])
        run["parameters"]["rounds"] = 99
        db.execute("UPDATE artifacts SET run=?", (json.dumps(run),))
    assert registry.score("trend", snapshot(dataset))["status"] == "BLOCKED"
