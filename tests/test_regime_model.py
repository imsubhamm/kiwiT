import copy
from dataclasses import replace
from datetime import datetime, time, timedelta
from decimal import Decimal
from types import MappingProxyType

import pytest

pytest.importorskip("xgboost")

from kiwit.marketdata.canonical import IST, Candle, Instrument, Session, TradingCalendar
from kiwit.marketdata.features import FeatureSnapshot
from kiwit.marketdata.history import HistoricalStore
from kiwit.ml.datasets import DatasetBuilder, LabelSpec, TimeSplits, _hash
from kiwit.ml.regime import CLASSES, RegimeConfig, RegimeModel, evaluate_walk_forward, train_regime

START = datetime(2026, 8, 1, 9, 15, tzinfo=IST)
DAY = timedelta(days=1)
INSTRUMENT = Instrument("SYNTHETIC_REGIME_FIXTURE")


@pytest.fixture(scope="module")
def dataset(tmp_path_factory):
    root = tmp_path_factory.mktemp("regime-fixture")
    store = HistoricalStore(root / "history.sqlite3")
    calendar = TradingCalendar(
        "SYNTHETIC_ONLY", tuple(Session((START + i * DAY).date(), closes=time(10, 5)) for i in range(12))
    )
    for day in range(12):
        kind = day % 4
        for i in range(50):
            at = START + day * DAY + timedelta(minutes=i)
            p = Decimal(100) + Decimal(i) / 10 * (1 if kind == 0 else -1 if kind == 1 else 0)
            width = Decimal(1) if kind == 3 else Decimal(".02")
            c = Candle(INSTRUMENT, at, at + timedelta(minutes=1), p, p + width, p - width, p, None, "SYNTHETIC_ONLY")
            store.record(
                [c], available_at=c.closed_at, raw_payload=f"synthetic-{day}-{i}".encode(), source="SYNTHETIC_ONLY"
            )
    return DatasetBuilder(store, calendar, labels=LabelSpec(horizon_minutes=5)).build(
        INSTRUMENT, START, TimeSplits(START + 4 * DAY, START + 8 * DAY, START + 12 * DAY), as_of=START + 13 * DAY
    )


def snapshot(dataset):
    row = next(r["features"] for r in dataset["rows"] if r["split"] == "test")
    return FeatureSnapshot(
        datetime.fromisoformat(row["as_of"]),
        INSTRUMENT,
        row["feature_version"],
        row["calendar_version"],
        row["input_digest"],
        MappingProxyType(row["values"]),
        MappingProxyType(row["unavailable"]),
        tuple(row["reasons"]),
        tuple(row["required"]),
    )


def refingerprint(data):
    data.pop("fingerprint", None)
    data["fingerprint"] = _hash(data)
    return data


def test_training_calibration_metrics_and_artifact_roundtrip(dataset, tmp_path):
    config = RegimeConfig(rounds=10)
    model = train_regime(dataset, config=config)
    report = model.artifact
    assert report["dataset_fingerprint"] == dataset["fingerprint"]
    assert report["calibration_fit_split"] == "validation"
    for split in ("train", "validation", "test"):
        metrics = report["evaluation"][split]
        assert metrics["calibrated"]["rows"] > 0
        assert 0 <= metrics["calibrated"]["brier_score"] <= 2
        assert 0 <= metrics["calibrated"]["expected_calibration_error"] <= 1
        assert sum(b["count"] for b in metrics["calibrated"]["reliability_bins"]) == metrics["calibrated"]["rows"]
        assert "rules" in metrics
    assert (
        report["evaluation"]["validation"]["calibrated"]["log_loss"]
        <= report["evaluation"]["validation"]["raw"]["log_loss"] + 1e-9
    )
    assert sum(report["feature_importance_gain"].values()) == pytest.approx(1)
    before = model.predict(snapshot(dataset))
    assert set(before["scores"]) == set(CLASSES)
    assert sum(before["scores"].values()) == pytest.approx(1)
    path = model.save(tmp_path)
    assert RegimeModel.load(path).predict(snapshot(dataset)) == before
    assert model.save(tmp_path) == path
    assert train_regime(dataset, config=config).artifact["fingerprint"] == report["fingerprint"]


def test_test_labels_do_not_change_model_or_temperature(dataset):
    config = RegimeConfig(rounds=3)
    first = train_regime(dataset, config=config)
    changed = copy.deepcopy(dataset)
    for row in changed["rows"]:
        if row["split"] == "test":
            row["labels"]["regime"] = "RANGE"
    second = train_regime(refingerprint(changed), config=config)
    assert first.artifact["model_json"] == second.artifact["model_json"]
    assert first.artifact["temperature"] == second.artifact["temperature"]


def test_uncertainty_for_confidence_and_invalid_input(dataset):
    model = train_regime(dataset, config=RegimeConfig(rounds=2, minimum_confidence=1))
    assert model.predict(snapshot(dataset))["regime"] == "UNCERTAIN"
    invalid = replace(snapshot(dataset), reasons=("STALE_CANDLES",))
    assert model.predict(invalid)["scores"] == {}
    assert model.predict(replace(snapshot(dataset), version="wrong"))["regime"] == "UNCERTAIN"


def test_invalid_dataset_and_missing_regimes_rejected(dataset):
    changed = copy.deepcopy(dataset)
    changed["rows"][0]["labels"]["regime"] = "RANGE"
    with pytest.raises(ValueError, match="fingerprint"):
        train_regime(changed)
    changed = copy.deepcopy(dataset)
    for row in changed["rows"]:
        if row["split"] == "train":
            row["labels"]["regime"] = "RANGE"
    with pytest.raises(ValueError, match="four regimes"):
        train_regime(refingerprint(changed))
    changed = copy.deepcopy(dataset)
    changed["rows"][0]["label_end"] = changed["splits"]["train_end"]
    with pytest.raises(ValueError, match="leakage"):
        train_regime(refingerprint(changed))


def test_walkforward_reports_and_rejects_overlapping_tests(dataset):
    report = evaluate_walk_forward([dataset], config=RegimeConfig(rounds=2))
    assert len(report["folds"]) == 1
    assert 0 <= report["mean_test_macro_f1"] <= 1
    with pytest.raises(ValueError, match="overlap"):
        evaluate_walk_forward([dataset, dataset])


def test_invalid_configuration():
    with pytest.raises(ValueError):
        RegimeConfig(rounds=0)
    with pytest.raises(ValueError):
        RegimeConfig(minimum_confidence=float("nan"))


def test_training_command(dataset, tmp_path):
    import json
    import subprocess
    import sys
    from pathlib import Path

    source = tmp_path / "SYNTHETIC_DATASET.json"
    source.write_text(json.dumps(dataset))
    command = Path(__file__).resolve().parents[1] / "scripts/train_regime_model.py"
    run = subprocess.run(
        [sys.executable, str(command), "--dataset", str(source), "--output", str(tmp_path / "models"), "--rounds", "2"],
        check=True,
        capture_output=True,
        text=True,
    )
    result = json.loads(run.stdout)
    assert result["status"] == "RESEARCH_ONLY"
    assert RegimeModel.load(result["artifact"]).artifact["dataset_fingerprint"] == dataset["fingerprint"]
