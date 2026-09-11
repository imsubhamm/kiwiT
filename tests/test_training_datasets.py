import json
from dataclasses import replace
from datetime import datetime, time, timedelta
from decimal import Decimal

import pytest

from kiwit.marketdata.canonical import IST, Candle, Instrument, Session, TradingCalendar
from kiwit.marketdata.history import HistoricalStore
from kiwit.ml.datasets import DatasetBuilder, LabelSpec, TimeSplits, _labels

START = datetime(2026, 9, 7, 9, 15, tzinfo=IST)
MINUTE = timedelta(minutes=1)
DAY = timedelta(days=1)
INSTRUMENT = Instrument("TEST")
CALENDAR = TradingCalendar("test", tuple(Session((START + i * DAY).date(), closes=time(10, 35)) for i in range(4)))


def setup(tmp_path, *, late=False):
    store = HistoricalStore(tmp_path / "history.sqlite3")
    for day in range(4):
        for i in range(80):
            at = START + day * DAY + i * MINUTE
            p = Decimal(100) + Decimal(i) / 10
            c = Candle(INSTRUMENT, at, at + MINUTE, p, p + Decimal(".02"), p - Decimal(".02"), p, 100, "test")
            store.record(
                [c],
                available_at=START + 4 * DAY if late and day == 0 and i == 50 else c.closed_at,
                raw_payload=json.dumps(c.to_json_dict()).encode(),
                source="test",
            )
    return DatasetBuilder(store, CALENDAR, labels=LabelSpec(horizon_minutes=5))


def splits():
    return TimeSplits(START + DAY, START + 2 * DAY, START + 3 * DAY)


def test_deterministic_dataset_splits_balance_and_saved_artifact(tmp_path):
    builder = setup(tmp_path)
    first = builder.build(INSTRUMENT, START, splits(), as_of=START + 4 * DAY)
    second = builder.build(INSTRUMENT, START, splits(), as_of=START + 4 * DAY)
    assert first == second
    assert all(first["report"]["leakage_checks"].values())
    assert all(count > 0 for count in first["report"]["row_counts"].values())
    assert any("CLASS_IMBALANCE" in w for w in first["report"]["warnings"])
    for row in first["rows"]:
        at = datetime.fromisoformat(row["at"])
        assert datetime.fromisoformat(row["label_end"]) < splits().locate(at)[1]
        assert row["features"]["as_of"] == row["at"]
        assert row["features"]["values"]["return_1"] is not None
        assert row["labels"]["trend_success"] == 1
        assert row["labels"]["breakout_success"] == 1
        assert row["labels"]["mean_reversion_success"] == 0
    path = builder.save(first, tmp_path / "datasets")
    assert json.loads(path.read_text()) == first
    assert builder.save(first, path.parent) == path
    first["rows"][0]["labels"]["trend_success"] = 0
    with pytest.raises(ValueError, match="changed"):
        builder.save(first, path.parent)


def test_intraday_boundaries_purge_overlap_and_late_labels(tmp_path):
    builder = setup(tmp_path, late=True)
    boundaries = TimeSplits(START + 55 * MINUTE, START + 65 * MINUTE, START + 80 * MINUTE)
    data = builder.build(INSTRUMENT, START, boundaries, as_of=START + 4 * DAY)
    assert data["report"]["excluded"]["PURGED_SPLIT_HORIZON"] > 0
    assert data["report"]["excluded"]["MISSING_OR_LATE_LABEL_DATA"] > 0
    assert all(
        datetime.fromisoformat(r["label_end"]) < boundaries.train_end for r in data["rows"] if r["split"] == "train"
    )
    # Late candle cannot repair the feature gap in earlier snapshots, even when build time is later.
    assert not any(datetime.fromisoformat(r["at"]) >= START + 51 * MINUTE for r in data["rows"])


def test_walk_forward_is_chronological_and_reproducible(tmp_path):
    builder = setup(tmp_path)
    folds = builder.walk_forward(
        INSTRUMENT, START, train=DAY, validation=DAY, test=DAY, step=DAY, folds=2, as_of=START + 4 * DAY
    )
    assert len(folds) == 2
    assert folds[0]["splits"]["validation_end"] == folds[1]["splits"]["train_end"]
    assert folds[0]["fingerprint"] != folds[1]["fingerprint"]
    assert all(all(f["report"]["leakage_checks"].values()) for f in folds)


def test_label_semantics_and_ineligible_not_negative():
    at = START
    c = Candle(INSTRUMENT, at, at + MINUTE, 100, 101, 98, 99, 1, "test")
    values = {"ema_spread": -0.1, "ema_21": 98, "breakout_high_20": 0, "breakout_low_20": 0}
    labeled = _labels(values, Decimal(100), [c], LabelSpec(high_volatility_range="0.1"))
    assert labeled["regime"] == "DOWNTREND"
    assert labeled["trend_success"] == 1
    assert labeled["breakout_success"] is None
    assert labeled["mean_reversion_success"] == 1
    high_vol = _labels(values, Decimal(100), [c], LabelSpec())
    assert high_vol["regime"] == "HIGH_VOLATILITY"
    assert _labels(values, Decimal(100), [c], LabelSpec(cost_fraction="0.02"))["trend_success"] == 0


def test_future_input_changes_do_not_change_past_features(tmp_path):
    builder = setup(tmp_path)
    baseline = builder.build(INSTRUMENT, START, splits(), as_of=START + 4 * DAY)
    # Build an alternative immutable source version with one later candle changed.
    alt = HistoricalStore(tmp_path / "alternate.sqlite3")
    for c in builder.store.candles(INSTRUMENT, START, START + 4 * DAY, as_of=START + 4 * DAY):
        changed = replace(c, close=c.high) if c.opened_at == START + 60 * MINUTE else c
        alt.record(
            [changed], available_at=c.closed_at, raw_payload=json.dumps(changed.to_json_dict()).encode(), source="test"
        )
    revised = DatasetBuilder(alt, CALENDAR, labels=builder.labels).build(
        INSTRUMENT, START, splits(), as_of=START + 4 * DAY
    )
    before = lambda data: [
        r["features"] for r in data["rows"] if datetime.fromisoformat(r["at"]) <= START + 60 * MINUTE
    ]
    assert before(baseline) == before(revised)


def test_invalid_specs_splits_and_cutoffs_rejected(tmp_path):
    with pytest.raises(ValueError):
        LabelSpec(horizon_minutes=0)
    with pytest.raises(ValueError):
        LabelSpec(movement_threshold="NaN")
    with pytest.raises(ValueError):
        TimeSplits(START + DAY, START, START + 2 * DAY)
    with pytest.raises(ValueError):
        TimeSplits(START.replace(tzinfo=None), START + DAY, START + 2 * DAY)
    builder = DatasetBuilder(HistoricalStore(tmp_path / "empty.sqlite3"), CALENDAR)
    with pytest.raises(ValueError):
        builder.build(INSTRUMENT, START, splits(), as_of=START + DAY)
    empty = builder.build(INSTRUMENT, START, splits(), as_of=START + 4 * DAY)
    assert empty["report"]["row_counts"] == {"train": 0, "validation": 0, "test": 0}
    assert "EMPTY_TRAIN" in empty["report"]["warnings"]


def test_build_command_emits_verified_artifact(tmp_path):
    import subprocess
    import sys
    from pathlib import Path

    builder = setup(tmp_path)
    calendar_path = tmp_path / "calendar.json"
    calendar_path.write_text(
        json.dumps(
            {
                "version": CALENDAR.version,
                "sessions": [
                    {"day": str(s.day), "opens": s.opens.isoformat(), "closes": s.closes.isoformat()}
                    for s in CALENDAR.sessions
                ],
            }
        )
    )
    script = Path(__file__).resolve().parents[1] / "scripts/build_training_dataset.py"
    result = subprocess.run(
        [
            sys.executable,
            str(script),
            "--db",
            str(builder.store.path),
            "--calendar",
            str(calendar_path),
            "--output",
            str(tmp_path / "output"),
            "--symbol",
            "TEST",
            "--start",
            START.isoformat(),
            "--train-end",
            (START + DAY).isoformat(),
            "--validation-end",
            (START + 2 * DAY).isoformat(),
            "--test-end",
            (START + 3 * DAY).isoformat(),
            "--as-of",
            (START + 4 * DAY).isoformat(),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    report = json.loads(result.stdout)
    artifact = json.loads(Path(report["path"]).read_text())
    assert artifact["fingerprint"] == report["fingerprint"]
    assert artifact["report"]["row_counts"]["train"] > 0
