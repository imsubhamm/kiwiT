"""Breakout opportunity scoring and chronological cost-aware research evaluation."""

from __future__ import annotations

import json
import math
import platform
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from kiwit.marketdata.canonical import IST
from kiwit.marketdata.features import FEATURE_VERSION, FeatureSnapshot

from .datasets import _hash, _json
from .regime import FEATURES as BASE_FEATURES
from .regime import RegimeConfig, _runtime, _temperature, _validate
from .trend import _accepted, _binary_metrics, _trade_summary

MODEL_VERSION = "breakout-xgb-v1"
FEATURES = BASE_FEATURES + ("compression_20", "breakout_high_20", "breakout_low_20", "range_5m")


def _side(values):
    above, below = values.get("breakout_high_20"), values.get("breakout_low_20")
    if above is None or below is None or not math.isfinite(above) or not math.isfinite(below):
        raise ValueError("Missing breakout boundary features")
    return 1 if above > 0 else -1 if below < 0 else 0


@dataclass(frozen=True)
class BreakoutConfig(RegimeConfig):
    round_trip_cost: float = 0.0002
    round_trip_slippage: float = 0.0004

    def __post_init__(self):
        super().__post_init__()
        for value in (self.round_trip_cost, self.round_trip_slippage):
            if not math.isfinite(value) or not 0 <= value < 1:
                raise ValueError("Costs and slippage must be finite nonnegative return fractions")
        if self.round_trip_cost + self.round_trip_slippage >= 1:
            raise ValueError("Combined cost/slippage must be below one")


def _false_breakouts(y, probabilities, config):
    selected = _accepted(probabilities, config)
    failures = y == 0
    return {
        "eligible_breakouts": len(y),
        "failures": int(failures.sum()),
        "failure_rate": float(failures.mean()),
        "selected_breakouts": int(selected.sum()),
        "selected_failures": int((selected & failures).sum()),
        "selected_failure_rate": float(failures[selected].mean()) if selected.any() else None,
        "failures_filtered": int((~selected & failures).sum()),
    }


def _strategy(rows, selected, config):
    """Single overlapping-position exclusion across the full split; fixed endpoint exit."""
    candidates = sorted(zip(rows, selected), key=lambda pair: pair[0]["at"])
    trades, next_entry, skipped = [], None, 0
    for row, use in candidates:
        if not use:
            continue
        at = datetime.fromisoformat(row["at"])
        if next_entry is not None and at < next_entry:
            skipped += 1
            continue
        gross = float(row["labels"]["forward_return"]) * row["labels"]["breakout_side"]
        next_entry = datetime.fromisoformat(row["label_end"])
        trades.append(
            {
                "at": row["at"],
                "exit_at": row["label_end"],
                "day": at.astimezone(IST).date().isoformat(),
                "regime": row["labels"]["regime"],
                "direction": "LONG" if row["labels"]["breakout_side"] == 1 else "SHORT",
                "gross_return": gross,
                "net_return": gross - config.round_trip_cost - config.round_trip_slippage,
            }
        )
    return {**_trade_summary(trades), "overlap_signals_skipped": skipped, "trades_detail": trades}


def _evaluate(rows, probabilities, config):
    np, _ = _runtime()
    y = np.asarray([r["labels"]["breakout_success"] for r in rows])
    rule = np.asarray([r["features"]["values"]["adx_14"] >= 20 for r in rows])
    model_trades = _strategy(rows, _accepted(probabilities, config), config)
    rule_trades = _strategy(rows, rule, config)
    groups = {}
    for category, keys in (
        ("regime", [r["labels"]["regime"] for r in rows]),
        ("day", [datetime.fromisoformat(r["at"]).astimezone(IST).date().isoformat() for r in rows]),
    ):
        groups[category] = {}
        for key in sorted(set(keys)):
            mask = np.asarray([v == key for v in keys])
            groups[category][key] = {
                "classification": _binary_metrics(y[mask], probabilities[mask], config),
                "false_breakouts": _false_breakouts(y[mask], probabilities[mask], config),
                "model_strategy": _trade_summary([t for t in model_trades["trades_detail"] if t[category] == key]),
                "rule_strategy": _trade_summary([t for t in rule_trades["trades_detail"] if t[category] == key]),
            }
    return {
        "classification": _binary_metrics(y, probabilities, config),
        "false_breakouts": _false_breakouts(y, probabilities, config),
        "deterministic_rule_classification": _binary_metrics(y, rule.astype(float), config),
        "strategy": model_trades,
        "deterministic_rule_strategy": rule_trades,
        "by": groups,
    }


class BreakoutModel:
    def __init__(self, artifact: dict):
        body = dict(artifact)
        fingerprint = body.pop("fingerprint")
        if fingerprint != _hash(body) or artifact["model_version"] != MODEL_VERSION:
            raise ValueError("Breakout artifact fingerprint/version mismatch")
        if tuple(artifact["features"]) != FEATURES or artifact["feature_version"] != FEATURE_VERSION:
            raise ValueError("Breakout feature contract mismatch")
        if not 0 < artifact["temperature"] < 100:
            raise ValueError("Invalid calibration temperature")
        _, xgb = _runtime()
        self.config = BreakoutConfig(**artifact["config"])
        self.artifact = json.loads(_json(artifact))
        self._booster = xgb.Booster(params={"nthread": 1, "device": "cpu"})
        self._booster.load_model(bytearray(artifact["model_json"].encode()))

    def predict(self, snapshot: FeatureSnapshot) -> dict:
        result = {
            "direction": "NONE",
            "continuation_probability": None,
            "failure_probability": None,
            "model_version": MODEL_VERSION,
            "model_fingerprint": self.artifact["fingerprint"],
            "feature_version": snapshot.version,
            "calibration": {
                "method": "temperature",
                "temperature": self.artifact["temperature"],
                "minimum_confidence": self.config.minimum_confidence,
                "minimum_margin": self.config.minimum_margin,
            },
        }
        if snapshot.version != FEATURE_VERSION or not snapshot.ready:
            return {**result, "reason_codes": ["FEATURES_UNAVAILABLE_OR_VERSION_MISMATCH"]}
        values = [snapshot.values.get(f) for f in FEATURES]
        if any(v is None or not math.isfinite(v) for v in values):
            return {**result, "reason_codes": ["MISSING_MODEL_INPUT"]}
        side = _side(snapshot.values)
        if side == 0:
            return {**result, "reason_codes": ["NO_BREAKOUT"]}
        np, xgb = _runtime()
        p = self._booster.predict(xgb.DMatrix(np.asarray([values]), feature_names=list(FEATURES), nthread=1))
        calibrated = _temperature(np.column_stack((1 - p, p)), self.artifact["temperature"])[0, 1]
        accepted = bool(_accepted(calibrated, self.config))
        return {
            **result,
            "continuation_probability": float(calibrated),
            "failure_probability": float(1 - calibrated),
            "direction": ("LONG" if side > 0 else "SHORT") if accepted else "NONE",
            "reason_codes": ["BREAKOUT_CONTINUATION"] if accepted else ["LOW_OPPORTUNITY_CONFIDENCE"],
        }

    def save(self, directory: str | Path) -> Path:
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"breakout-{self.artifact['fingerprint']}.json"
        body = dict(self.artifact)
        fingerprint = body.pop("fingerprint")
        if _hash(body) != fingerprint:
            raise ValueError("Breakout artifact mutated after training")
        content = _json(self.artifact) + "\n"
        try:
            with path.open("x", encoding="utf-8") as stream:
                stream.write(content)
        except FileExistsError:
            if path.read_text() != content:
                raise ValueError("Existing breakout artifact differs") from None
        return path

    @classmethod
    def load(cls, path: str | Path):
        return cls(json.loads(Path(path).read_text()))


def train_breakout(dataset: dict, *, config: BreakoutConfig | None = None) -> BreakoutModel:
    config = config or BreakoutConfig()
    all_groups = _validate(dataset, require_all_regimes=False)
    groups, excluded = {}, {}
    for split, rows in all_groups.items():
        groups[split] = []
        for row in rows:
            side = _side(row["features"]["values"])
            if any(
                row["features"]["values"].get(f) is None or not math.isfinite(row["features"]["values"][f])
                for f in FEATURES
            ):
                raise ValueError("Selected breakout model feature missing or nonfinite")
            if row["labels"]["breakout_side"] != side:
                raise ValueError("Breakout label direction differs from contemporaneous breakout direction")
            label = row["labels"]["breakout_success"]
            if side == 0:
                if label is not None:
                    raise ValueError("Ineligible breakout must have null target")
                continue
            if type(label) is not int or label not in {0, 1}:
                raise ValueError("Breakout target must be binary")
            forward = float(row["labels"]["forward_return"])
            if not math.isfinite(forward):
                raise ValueError("Nonfinite forward return")
            groups[split].append(row)
        excluded[split] = len(rows) - len(groups[split])
    if any(not rows for rows in groups.values()):
        raise ValueError("Every split needs eligible breakout samples")
    if {r["labels"]["breakout_success"] for r in groups["train"]} != {0, 1}:
        raise ValueError("Breakout training requires successes and failures")
    np, xgb = _runtime()
    matrices = {
        name: xgb.DMatrix(
            np.asarray([[r["features"]["values"][f] for f in FEATURES] for r in rows]),
            label=[r["labels"]["breakout_success"] for r in rows],
            feature_names=list(FEATURES),
            nthread=1,
        )
        for name, rows in groups.items()
    }
    params = {
        "objective": "binary:logistic",
        "eval_metric": "logloss",
        "tree_method": "hist",
        "device": "cpu",
        "nthread": 1,
        "seed": config.seed,
        "max_depth": config.max_depth,
        "eta": config.learning_rate,
        "subsample": 1.0,
        "colsample_bytree": 1.0,
    }
    booster = xgb.train(params, matrices["train"], num_boost_round=config.rounds)
    raw = {name: booster.predict(matrix) for name, matrix in matrices.items()}
    validation_y = np.asarray([r["labels"]["breakout_success"] for r in groups["validation"]])

    def calibrate(p, t):
        return _temperature(np.column_stack((1 - p, p)), t)[:, 1]

    temperature = min(
        (0.5, 0.75, 1.0, 1.5, 2.0, 3.0),
        key=lambda t: (_binary_metrics(validation_y, calibrate(raw["validation"], t), config)["log_loss"], abs(t - 1)),
    )
    evaluation = {
        name: {
            "raw_classification": _binary_metrics(
                np.asarray([r["labels"]["breakout_success"] for r in rows]), raw[name], config
            ),
            "calibrated": _evaluate(rows, calibrate(raw[name], temperature), config),
        }
        for name, rows in groups.items()
    }
    gains = booster.get_score(importance_type="gain")
    total = sum(gains.values())
    artifact = {
        "model_version": MODEL_VERSION,
        "feature_version": FEATURE_VERSION,
        "features": list(FEATURES),
        "config": asdict(config),
        "parameters": params,
        "temperature": temperature,
        "calibration_fit_split": "validation",
        "dataset_fingerprint": dataset["fingerprint"],
        "dataset_version": dataset["dataset_version"],
        "calendar_version": dataset["calendar_version"],
        "label_spec": dataset["label_spec"],
        "target_definition": "breakout_success: signed horizon return minus label cost >= movement threshold; failure is its complement; no boundary break is ineligible",
        "splits": dataset["splits"],
        "excluded_ineligible": excluded,
        "evaluation": evaluation,
        "feature_importance_gain": {f: gains.get(f, 0) / total if total else 0 for f in FEATURES},
        "runtime": {
            "xgboost": xgb.__version__,
            "numpy": np.__version__,
            "python": platform.python_version(),
            "platform": platform.platform(),
        },
        "training_class_counts": dict(Counter(str(r["labels"]["breakout_success"]) for r in groups["train"])),
        "evaluation_assumptions": {
            "entry": "decision candle close",
            "exit": "fixed label horizon close",
            "overlap": "one position at a time",
            "sizing": "unit return; no position size",
            "costs": "round-trip fractions subtracted once; no compounding",
        },
        "promotion_status": "RESEARCH_ONLY",
        "model_json": booster.save_raw(raw_format="json").decode(),
    }
    return BreakoutModel({**artifact, "fingerprint": _hash(artifact)})
