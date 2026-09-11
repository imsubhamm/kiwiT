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
from kiwit.ml.trend import TrendConfig, TrendModel, _strategy, train_trend

START = datetime(2026, 8, 1, 9, 15, tzinfo=IST)
DAY, MINUTE = timedelta(days=1), timedelta(minutes=1)
INSTRUMENT = Instrument("SYNTHETIC_TREND_FIXTURE")


@pytest.fixture(scope="module")
def dataset(tmp_path_factory):
    root = tmp_path_factory.mktemp("trend-fixture")
    store = HistoricalStore(root / "history.sqlite3")
    calendar = TradingCalendar(
        "SYNTHETIC_ONLY", tuple(Session((START + i * DAY).date(), closes=time(10, 5)) for i in range(6))
    )
    for day in range(6):
        sign = 1 if day % 2 == 0 else -1
        for i in range(50):
            at = START + day * DAY + i * MINUTE
            p = Decimal(100) + Decimal(sign) / 10 * (i if i < 40 else 80 - i)
            c = Candle(
                INSTRUMENT, at, at + MINUTE, p, p + Decimal(".02"), p - Decimal(".02"), p, None, "SYNTHETIC_ONLY"
            )
            store.record(
                [c], available_at=c.closed_at, raw_payload=f"fixture-{day}-{i}".encode(), source="SYNTHETIC_ONLY"
            )
    return DatasetBuilder(store, calendar, labels=LabelSpec(horizon_minutes=5)).build(
        INSTRUMENT, START, TimeSplits(START + 2 * DAY, START + 4 * DAY, START + 6 * DAY), as_of=START + 7 * DAY
    )


def snapshot(dataset, side=1):
    row = next(r["features"] for r in dataset["rows"] if r["split"] == "test" and r["labels"]["trend_side"] == side)
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


def test_trend_model_metrics_regime_period_costs_and_roundtrip(dataset, tmp_path):
    config = TrendConfig(rounds=8)
    model = train_trend(dataset, config=config)
    for split in ("train", "validation", "test"):
        report = model.artifact["evaluation"][split]["calibrated"]
        assert report["classification"]["rows"] > 0
        assert report["by"]["regime"]
        assert len(report["by"]["day"]) == 2
        assert report["strategy"]["net_return_sum"] <= report["strategy"]["gross_return_sum"]
        assert "deterministic_rule_strategy" in report
    evaluation = model.artifact["evaluation"]["validation"]
    assert evaluation["calibrated"]["classification"]["log_loss"] <= evaluation["raw_classification"]["log_loss"] + 1e-9
    prediction = model.predict(snapshot(dataset))
    assert prediction["direction"] in {"LONG", "SHORT", "NONE"}
    assert 0 <= prediction["opportunity_probability"] <= 1
    assert prediction["calibration"]["method"] == "temperature"
    path = model.save(tmp_path)
    assert TrendModel.load(path).predict(snapshot(dataset)) == prediction
    assert train_trend(dataset, config=config).artifact["fingerprint"] == model.artifact["fingerprint"]


def test_invalid_and_uncertain_inputs_return_none(dataset):
    model = train_trend(dataset, config=TrendConfig(rounds=2, minimum_confidence=1))
    assert model.predict(snapshot(dataset))["direction"] == "NONE"
    assert model.predict(replace(snapshot(dataset), version="wrong"))["opportunity_probability"] is None
    assert model.predict(replace(snapshot(dataset), reasons=("STALE",)))["direction"] == "NONE"


def test_direction_is_only_from_contemporaneous_features(dataset):
    model = train_trend(dataset, config=TrendConfig(rounds=2, minimum_confidence=0, minimum_margin=0))
    for side in (1, -1):
        prediction = model.predict(snapshot(dataset, side=side))
        assert prediction["direction"] in ({"LONG", "NONE"} if side == 1 else {"SHORT", "NONE"})


def test_test_targets_do_not_tune_model_or_calibration(dataset):
    config = TrendConfig(rounds=2)
    first = train_trend(dataset, config=config)
    changed = copy.deepcopy(dataset)
    for row in changed["rows"]:
        if row["split"] == "test":
            row["labels"]["trend_success"] = 1 - row["labels"]["trend_success"]
    second = train_trend(refingerprint(changed), config=config)
    assert first.artifact["model_json"] == second.artifact["model_json"]
    assert first.artifact["temperature"] == second.artifact["temperature"]


def test_single_class_and_future_direction_leak_rejected(dataset):
    changed = copy.deepcopy(dataset)
    for row in changed["rows"]:
        if row["split"] == "train":
            row["labels"]["trend_success"] = 1
    with pytest.raises(ValueError, match="successes and failures"):
        train_trend(refingerprint(changed))
    changed = copy.deepcopy(dataset)
    changed["rows"][0]["labels"]["trend_side"] *= -1
    with pytest.raises(ValueError, match="direction"):
        train_trend(refingerprint(changed))


def test_overlapping_signals_not_double_counted_and_costs_applied_once():
    rows = [
        {
            "at": (START + i * MINUTE).isoformat(),
            "label_end": (START + (i + 5) * MINUTE).isoformat(),
            "labels": {"forward_return": "0.01", "trend_side": 1, "regime": "UPTREND"},
        }
        for i in range(11)
    ]
    config = TrendConfig(round_trip_cost=0.001, round_trip_slippage=0.002)
    result = _strategy(rows, [True] * len(rows), config)
    assert result["trades"] == 3
    assert result["overlap_signals_skipped"] == 8
    assert result["gross_return_sum"] == pytest.approx(0.03)
    assert result["net_return_sum"] == pytest.approx(0.021)
    for before, after in zip(result["trades_detail"], result["trades_detail"][1:]):
        assert before["exit_at"] <= after["at"]


def test_invalid_costs():
    with pytest.raises(ValueError):
        TrendConfig(round_trip_slippage=-1)
    with pytest.raises(ValueError):
        TrendConfig(round_trip_cost=float("nan"))


def test_training_cli(dataset, tmp_path):
    import json
    import subprocess
    import sys
    from pathlib import Path

    source = tmp_path / "dataset.json"
    source.write_text(json.dumps(dataset))
    result = subprocess.run(
        [
            sys.executable,
            "scripts/train_trend_model.py",
            "--dataset",
            str(source),
            "--output",
            str(tmp_path / "models"),
            "--rounds",
            "2",
        ],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        check=True,
    )
    report = json.loads(result.stdout)
    assert report["status"] == "RESEARCH_ONLY"
    assert TrendModel.load(report["artifact"]).artifact["dataset_fingerprint"] == dataset["fingerprint"]
