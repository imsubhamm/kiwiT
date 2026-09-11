"""Deterministic supervised datasets with chronological purging and no imputation."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from kiwit.marketdata.canonical import IST, Instrument, TradingCalendar, utc
from kiwit.marketdata.features import DEFAULT_REQUIRED, FEATURE_VERSION, FeatureEngine
from kiwit.marketdata.history import HistoricalStore, replay_sessions

DATASET_VERSION = "supervised-dataset-v1"
LABEL_VERSION = "strategy-labels-v1"
_MINUTE = timedelta(minutes=1)
_EPSILON = timedelta(microseconds=1)


def _json(data) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(data) -> str:
    return hashlib.sha256(_json(data).encode()).hexdigest()


@dataclass(frozen=True)
class LabelSpec:
    """Research target definitions; thresholds are not evidence of tradability."""

    horizon_minutes: int = 15
    movement_threshold: str = "0.001"
    high_volatility_range: str = "0.01"
    reversion_distance: str = "0.002"
    cost_fraction: str = "0"
    version: str = LABEL_VERSION

    def __post_init__(self):
        if (
            self.version != LABEL_VERSION
            or type(self.horizon_minutes) is not int
            or not 1 <= self.horizon_minutes <= 60
        ):
            raise ValueError("Unsupported label version or horizon")
        for field in ("movement_threshold", "high_volatility_range", "reversion_distance", "cost_fraction"):
            value = Decimal(getattr(self, field))
            if not value.is_finite() or value < 0 or value >= 1 or (field != "cost_fraction" and value == 0):
                raise ValueError("Label thresholds must be finite fractions in range")
        if Decimal(self.high_volatility_range) <= Decimal(self.movement_threshold):
            raise ValueError("Volatility threshold must exceed directional threshold")


@dataclass(frozen=True)
class TimeSplits:
    train_end: datetime
    validation_end: datetime
    test_end: datetime

    def __post_init__(self):
        for name in ("train_end", "validation_end", "test_end"):
            object.__setattr__(self, name, utc(getattr(self, name)))
        if not self.train_end < self.validation_end < self.test_end:
            raise ValueError("Split boundaries must be chronological")

    def locate(self, at: datetime) -> tuple[str, datetime]:
        for name, end in (("train", self.train_end), ("validation", self.validation_end), ("test", self.test_end)):
            if at < end:
                return name, end
        raise ValueError("Sample is outside split range")

    def to_json_dict(self):
        return {key: value.isoformat() for key, value in asdict(self).items()}


def _labels(features, base: Decimal, future, spec: LabelSpec) -> dict:
    movement = future[-1].close / base - 1
    threshold = Decimal(spec.movement_threshold)
    cost = Decimal(spec.cost_fraction)
    amplitude = (max(c.high for c in future) - min(c.low for c in future)) / base
    regime = (
        "HIGH_VOLATILITY"
        if amplitude >= Decimal(spec.high_volatility_range)
        else ("UPTREND" if movement >= threshold else "DOWNTREND" if movement <= -threshold else "RANGE")
    )
    spread = features["ema_spread"]
    trend_side = 1 if spread is not None and spread > 0 else -1 if spread is not None and spread < 0 else 0
    above, below = features["breakout_high_20"], features["breakout_low_20"]
    breakout_side = 1 if above is not None and above > 0 else -1 if below is not None and below < 0 else 0
    ema = features["ema_21"]
    deviation = base / Decimal(str(ema)) - 1 if ema is not None else None
    reversion_side = (
        (-1 if deviation > 0 else 1)
        if (deviation is not None and abs(deviation) >= Decimal(spec.reversion_distance))
        else 0
    )
    reversion_success = None
    if reversion_side:
        closer = abs(future[-1].close / Decimal(str(ema)) - 1) < abs(deviation)
        reversion_success = int(closer and movement * reversion_side - cost >= threshold)
    return {
        "regime": regime,
        "trend_success": int(movement * trend_side - cost >= threshold) if trend_side else None,
        "breakout_success": int(movement * breakout_side - cost >= threshold) if breakout_side else None,
        "mean_reversion_success": reversion_success,
        "trend_side": trend_side,
        "breakout_side": breakout_side,
        "mean_reversion_side": reversion_side,
        "forward_return": str(movement),
    }


class DatasetBuilder:
    def __init__(
        self,
        store: HistoricalStore,
        calendar: TradingCalendar,
        *,
        labels: LabelSpec | None = None,
        required: tuple[str, ...] = DEFAULT_REQUIRED,
    ):
        self.store, self.calendar, self.labels = store, calendar, labels or LabelSpec()
        self.engine = FeatureEngine(calendar, required=required)

    def build(self, instrument: Instrument, start: datetime, splits: TimeSplits, *, as_of: datetime) -> dict:
        start, as_of = utc(start), utc(as_of)
        if not start < splits.train_end or splits.test_end > as_of:
            raise ValueError("Build needs a training range and a cutoff at/after test end")
        rows = []
        excluded = Counter()
        horizon = timedelta(minutes=self.labels.horizon_minutes)
        first_session = self.calendar.session(start.astimezone(IST).date())
        replay_start = min(start, first_session.bounds()[0]) if first_session else start
        for frame in replay_sessions(self.store, self.calendar, instrument, replay_start, splits.test_end):
            at = frame.as_of
            if at < start or at >= splits.test_end:
                continue
            split, split_end = splits.locate(at)
            snapshot = self.engine.compute(frame.result, instrument, at)
            if not snapshot.ready:
                excluded["FEATURES_UNAVAILABLE"] += 1
                continue
            target = at + horizon
            # Strict end-exclusive splits: neither targets nor label availability may cross them.
            if target >= split_end:
                excluded["PURGED_SPLIT_HORIZON"] += 1
                continue
            session = self.calendar.session(at.astimezone(IST).date())
            if target > session.bounds()[1]:
                excluded["OUTCOME_OUTSIDE_SESSION"] += 1
                continue
            label_cutoff = min(as_of, split_end - _EPSILON)
            future = self.store.candles(instrument, at, target, as_of=label_cutoff)
            if tuple(c.opened_at for c in future) != tuple(
                at + i * _MINUTE for i in range(self.labels.horizon_minutes)
            ):
                excluded["MISSING_OR_LATE_LABEL_DATA"] += 1
                continue
            label = _labels(snapshot.values, frame.result.candles[-1].close, future, self.labels)
            rows.append(
                {
                    "split": split,
                    "at": at.isoformat(),
                    "label_end": target.isoformat(),
                    "label_checked_as_of": label_cutoff.isoformat(),
                    "features": snapshot.to_json_dict(),
                    "labels": label,
                    "outcome_digest": _hash([c.to_json_dict() for c in future]),
                }
            )
        counts = {name: sum(r["split"] == name for r in rows) for name in ("train", "validation", "test")}
        classes = {}
        warnings = []
        for split, count in counts.items():
            classes[split] = {}
            if not count:
                warnings.append(f"EMPTY_{split.upper()}")
            for label in ("regime", "trend_success", "breakout_success", "mean_reversion_success"):
                selected = [r["labels"][label] for r in rows if r["split"] == split]
                distribution = Counter(str(v) for v in selected if v is not None)
                eligible = sum(distribution.values())
                majority = max(distribution.values()) / eligible if eligible else None
                classes[split][label] = {
                    "counts": dict(sorted(distribution.items())),
                    "ineligible": sum(v is None for v in selected),
                    "majority_fraction": majority,
                }
                if not eligible:
                    warnings.append(f"NO_ELIGIBLE_LABEL:{split}:{label}")
                elif majority > 0.8:
                    warnings.append(f"CLASS_IMBALANCE:{split}:{label}")
        checks = {
            "unique_timestamps": len({r["at"] for r in rows}) == len(rows),
            "features_at_decision_time": all(r["features"]["as_of"] == r["at"] for r in rows),
            "labels_after_features": all(r["at"] < r["label_end"] for r in rows),
            "horizons_within_split": all(
                datetime.fromisoformat(r["label_end"]) < splits.locate(datetime.fromisoformat(r["at"]))[1] for r in rows
            ),
            "availability_within_split": all(
                datetime.fromisoformat(r["label_checked_as_of"]) < splits.locate(datetime.fromisoformat(r["at"]))[1]
                for r in rows
            ),
        }
        if not all(checks.values()):
            raise ValueError("Dataset leakage invariant failed")
        result = {
            "dataset_version": DATASET_VERSION,
            "feature_version": FEATURE_VERSION,
            "calendar_version": self.calendar.version,
            "calendar": [
                {"day": str(s.day), "opens": s.opens.isoformat(), "closes": s.closes.isoformat()}
                for s in sorted(self.calendar.sessions, key=lambda s: s.day)
            ],
            "instrument": asdict(instrument),
            "label_spec": asdict(self.labels),
            "required_features": list(self.engine.required),
            "start": start.isoformat(),
            "as_of": as_of.isoformat(),
            "splits": splits.to_json_dict(),
            "rows": rows,
            "report": {
                "row_counts": counts,
                "excluded": dict(sorted(excluded.items())),
                "class_balance": classes,
                "warnings": sorted(warnings),
                "leakage_checks": checks,
            },
        }
        return {**result, "fingerprint": _hash(result)}

    @staticmethod
    def save(dataset: dict, directory: str | Path) -> Path:
        data = dict(dataset)
        fingerprint = data.pop("fingerprint")
        if fingerprint != _hash(data):
            raise ValueError("Dataset changed after fingerprint generation")
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        destination = directory / f"dataset-{fingerprint}.json"
        body = _json(dataset) + "\n"
        try:
            with destination.open("x", encoding="utf-8") as stream:
                stream.write(body)
        except FileExistsError:
            if destination.read_text() != body:
                raise ValueError("Existing dataset artifact differs") from None
        return destination

    def walk_forward(
        self,
        instrument: Instrument,
        start: datetime,
        *,
        train: timedelta,
        validation: timedelta,
        test: timedelta,
        step: timedelta,
        folds: int,
        as_of: datetime,
    ) -> list[dict]:
        if type(folds) is not int or folds < 1 or any(d <= timedelta(0) for d in (train, validation, test, step)):
            raise ValueError("Walk-forward durations and fold count must be positive")
        start, as_of = utc(start), utc(as_of)
        if start + (folds - 1) * step + train + validation + test > as_of:
            raise ValueError("Walk-forward windows exceed available cutoff")
        return [
            self.build(
                instrument,
                start + i * step,
                TimeSplits(
                    start + i * step + train,
                    start + i * step + train + validation,
                    start + i * step + train + validation + test,
                ),
                as_of=as_of,
            )
            for i in range(folds)
        ]
