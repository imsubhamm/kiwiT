"""CPU XGBoost regime baseline, held-out evaluation and calibrated abstention."""

from __future__ import annotations

import json
import math
import platform
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from kiwit.marketdata.features import FEATURE_VERSION, FeatureSnapshot

from .datasets import DATASET_VERSION, LABEL_VERSION, TimeSplits, _hash, _json

MODEL_VERSION = "regime-xgb-v1"
CLASSES = ("TREND_UP", "TREND_DOWN", "RANGE", "HIGH_VOLATILITY")
TARGETS = ("UPTREND", "DOWNTREND", "RANGE", "HIGH_VOLATILITY")
FEATURES = (
    "return_1",
    "momentum_5",
    "ema_spread",
    "ema_9_slope",
    "rsi_14",
    "macd_histogram",
    "atr_14",
    "realized_volatility_20",
    "adx_14",
    "range_fraction",
    "return_5m",
    "session_fraction",
)


@dataclass(frozen=True)
class RegimeConfig:
    rounds: int = 40
    max_depth: int = 3
    learning_rate: float = 0.1
    seed: int = 7
    minimum_confidence: float = 0.6
    minimum_margin: float = 0.1

    def __post_init__(self):
        if type(self.rounds) is not int or not 1 <= self.rounds <= 500:
            raise ValueError("Rounds must be between 1 and 500")
        if type(self.max_depth) is not int or not 1 <= self.max_depth <= 8:
            raise ValueError("Depth must be between 1 and 8")
        if (
            not 0 < self.learning_rate <= 1
            or not 0 <= self.minimum_confidence <= 1
            or not 0 <= self.minimum_margin <= 1
        ):
            raise ValueError("Invalid rate or uncertainty policy")
        if type(self.seed) is not int or not 0 <= self.seed < 2**31:
            raise ValueError("Invalid random seed")


def _runtime():
    import numpy as np
    import xgboost as xgb

    return np, xgb


def _temperature(probabilities, temperature):
    np, _ = _runtime()
    logits = np.log(np.clip(probabilities, 1e-15, 1)) / temperature
    logits -= logits.max(axis=1, keepdims=True)
    exp = np.exp(logits)
    return exp / exp.sum(axis=1, keepdims=True)


def _metrics(y, p, config):
    np, _ = _runtime()
    pred = p.argmax(axis=1)
    matrix = np.zeros((4, 4), dtype=int)
    for actual, predicted in zip(y, pred):
        matrix[int(actual), int(predicted)] += 1
    f1 = []
    for i in range(4):
        tp = matrix[i, i]
        denominator = matrix[i, :].sum() + matrix[:, i].sum()
        f1.append(float(2 * tp / denominator) if denominator else 0.0)
    confidence = p.max(axis=1)
    sorted_p = np.sort(p, axis=1)
    accepted = (confidence >= config.minimum_confidence) & (
        (sorted_p[:, -1] - sorted_p[:, -2]) >= config.minimum_margin
    )
    correct = pred == y
    bins = []
    ece = 0.0
    for index in range(10):
        mask = np.minimum((confidence * 10).astype(int), 9) == index
        count = int(mask.sum())
        mean_confidence = float(confidence[mask].mean()) if count else None
        accuracy = float(correct[mask].mean()) if count else None
        if count:
            ece += count / len(y) * abs(mean_confidence - accuracy)
        bins.append(
            {
                "lower": index / 10,
                "upper": (index + 1) / 10,
                "count": count,
                "confidence": mean_confidence,
                "accuracy": accuracy,
            }
        )
    return {
        "rows": len(y),
        "accuracy": float(correct.mean()),
        "macro_f1": sum(f1) / 4,
        "per_class_f1": dict(zip(CLASSES, f1)),
        "confusion_matrix": matrix.tolist(),
        "class_counts": {name: int((y == i).sum()) for i, name in enumerate(CLASSES)},
        "log_loss": float(-np.log(np.clip(p[np.arange(len(y)), y], 1e-15, 1)).mean()),
        "brier_score": float(((p - np.eye(4)[y]) ** 2).sum(axis=1).mean()),
        "expected_calibration_error": ece,
        "reliability_bins": bins,
        "coverage": float(accepted.mean()),
        "accepted_accuracy": float(correct[accepted].mean()) if accepted.any() else None,
    }


def _validate(dataset, *, require_all_regimes: bool = True):
    body = dict(dataset)
    fingerprint = body.pop("fingerprint")
    if fingerprint != _hash(body) or dataset["dataset_version"] != DATASET_VERSION:
        raise ValueError("Dataset fingerprint/version mismatch")
    if dataset["feature_version"] != FEATURE_VERSION or dataset["label_spec"]["version"] != LABEL_VERSION:
        raise ValueError("Dataset feature/label version mismatch")
    splits = TimeSplits(**{key: datetime.fromisoformat(value) for key, value in dataset["splits"].items()})
    groups = {name: [] for name in ("train", "validation", "test")}
    seen = set()
    for row in dataset["rows"]:
        at, target = datetime.fromisoformat(row["at"]), datetime.fromisoformat(row["label_end"])
        name, end = splits.locate(at)
        if row["at"] in seen or row["split"] != name or not at < target < end:
            raise ValueError("Dataset split leakage or duplicate timestamp")
        if not target <= datetime.fromisoformat(row["label_checked_as_of"]) < end:
            raise ValueError("Label availability crosses a split")
        if (
            row["features"]["as_of"] != row["at"]
            or row["features"]["feature_version"] != FEATURE_VERSION
            or not row["features"]["ready"]
        ):
            raise ValueError("Invalid point-in-time feature snapshot")
        seen.add(row["at"])
        if row["labels"]["regime"] not in TARGETS:
            raise ValueError("Unknown regime target")
        if any(
            row["features"]["values"].get(name) is None or not math.isfinite(row["features"]["values"][name])
            for name in FEATURES
        ):
            raise ValueError("Selected model feature is missing or nonfinite")
        groups[name].append(row)
    if any(not rows for rows in groups.values()):
        raise ValueError("All chronological splits need samples")
    if require_all_regimes and {r["labels"]["regime"] for r in groups["train"]} != set(TARGETS):
        raise ValueError("Training split must represent all four regimes")
    return groups


class RegimeModel:
    def __init__(self, artifact: dict):
        body = dict(artifact)
        fingerprint = body.pop("fingerprint")
        if fingerprint != _hash(body) or artifact["model_version"] != MODEL_VERSION:
            raise ValueError("Regime artifact fingerprint/version mismatch")
        if tuple(artifact["features"]) != FEATURES or artifact["feature_version"] != FEATURE_VERSION:
            raise ValueError("Unsupported model feature contract")
        _np, xgb = _runtime()
        if tuple(artifact["classes"]) != CLASSES or not 0 < artifact["temperature"] < 100:
            raise ValueError("Invalid class or calibration contract")
        self.artifact = json.loads(_json(artifact))
        self.config = RegimeConfig(**artifact["config"])
        self._booster = xgb.Booster(params={"nthread": 1, "device": "cpu"})
        self._booster.load_model(bytearray(artifact["model_json"].encode()))

    def predict(self, snapshot: FeatureSnapshot) -> dict:
        uncertain = {
            "regime": "UNCERTAIN",
            "scores": {},
            "model_version": MODEL_VERSION,
            "model_fingerprint": self.artifact["fingerprint"],
            "feature_version": snapshot.version,
        }
        if snapshot.version != self.artifact["feature_version"] or not snapshot.ready:
            return {**uncertain, "reason_codes": ["FEATURES_UNAVAILABLE_OR_VERSION_MISMATCH"]}
        values = [snapshot.values.get(name) for name in FEATURES]
        if any(value is None or not math.isfinite(value) for value in values):
            return {**uncertain, "reason_codes": ["MISSING_MODEL_INPUT"]}
        np, xgb = _runtime()
        raw = self._booster.predict(xgb.DMatrix(np.asarray([values]), feature_names=list(FEATURES), nthread=1))
        p = _temperature(raw, self.artifact["temperature"])[0]
        order = np.argsort(p)
        uncertain_state = (
            p[order[-1]] < self.config.minimum_confidence or p[order[-1]] - p[order[-2]] < self.config.minimum_margin
        )
        return {
            **uncertain,
            "regime": "UNCERTAIN" if uncertain_state else CLASSES[int(order[-1])],
            "scores": dict(zip(CLASSES, map(float, p))),
            "reason_codes": ["LOW_CONFIDENCE_OR_MARGIN"] if uncertain_state else ["REGIME_SCORED"],
        }

    def save(self, directory: str | Path) -> Path:
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"regime-{self.artifact['fingerprint']}.json"
        content = _json(self.artifact) + "\n"
        try:
            with path.open("x", encoding="utf-8") as stream:
                stream.write(content)
        except FileExistsError:
            if path.read_text() != content:
                raise ValueError("Existing model artifact differs") from None
        return path

    @classmethod
    def load(cls, path: str | Path):
        return cls(json.loads(Path(path).read_text()))


def train_regime(dataset: dict, *, config: RegimeConfig | None = None) -> RegimeModel:
    config = config or RegimeConfig()
    groups = _validate(dataset)
    np, xgb = _runtime()
    matrices, targets = {}, {}
    for name, rows in groups.items():
        x = np.asarray([[r["features"]["values"][f] for f in FEATURES] for r in rows], dtype=float)
        y = np.asarray([TARGETS.index(r["labels"]["regime"]) for r in rows], dtype=int)
        matrices[name] = xgb.DMatrix(x, label=y, feature_names=list(FEATURES), nthread=1)
        targets[name] = y
    params = {
        "objective": "multi:softprob",
        "num_class": 4,
        "eval_metric": "mlogloss",
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
    # Fixed grid; validation fits calibration only. Test data never tunes the model or temperature.
    candidates = (0.5, 0.75, 1.0, 1.5, 2.0, 3.0)
    temperature = min(
        candidates,
        key=lambda t: (
            _metrics(targets["validation"], _temperature(raw["validation"], t), config)["log_loss"],
            abs(t - 1),
        ),
    )
    train_vol = [r["features"]["values"]["realized_volatility_20"] for r in groups["train"]]
    volatility_threshold = float(np.quantile(train_vol, 0.9))
    rule_metrics = {}
    for name, rows in groups.items():
        classes = []
        for row in rows:
            v = row["features"]["values"]
            label = (
                3
                if v["realized_volatility_20"] > volatility_threshold
                else (
                    0
                    if v["adx_14"] >= 20 and v["ema_spread"] > 0
                    else 1
                    if v["adx_14"] >= 20 and v["ema_spread"] < 0
                    else 2
                )
            )
            classes.append(label)
        rule_metrics[name] = _metrics(targets[name], np.eye(4)[classes], config)
    evaluation = {
        name: {
            "raw": _metrics(targets[name], raw[name], config),
            "calibrated": _metrics(targets[name], _temperature(raw[name], temperature), config),
            "rules": rule_metrics[name],
        }
        for name in groups
    }
    gains = booster.get_score(importance_type="gain")
    total = sum(gains.values())
    importance = {name: gains.get(name, 0) / total if total else 0.0 for name in FEATURES}
    artifact = {
        "model_version": MODEL_VERSION,
        "feature_version": FEATURE_VERSION,
        "features": list(FEATURES),
        "classes": list(CLASSES),
        "config": asdict(config),
        "dataset_fingerprint": dataset["fingerprint"],
        "dataset_version": dataset["dataset_version"],
        "calendar_version": dataset["calendar_version"],
        "label_spec": dataset["label_spec"],
        "splits": dataset["splits"],
        "runtime": {
            "xgboost": xgb.__version__,
            "numpy": np.__version__,
            "python": platform.python_version(),
            "platform": platform.platform(),
        },
        "parameters": params,
        "temperature": temperature,
        "calibration_fit_split": "validation",
        "evaluation": evaluation,
        "rule_volatility_threshold": volatility_threshold,
        "feature_importance_gain": importance,
        "diagnostics": {
            "train_ranges": {
                f: [
                    min(r["features"]["values"][f] for r in groups["train"]),
                    max(r["features"]["values"][f] for r in groups["train"]),
                ]
                for f in FEATURES
            },
            "test_beats_rules_macro_f1": evaluation["test"]["calibrated"]["macro_f1"]
            > evaluation["test"]["rules"]["macro_f1"],
            "promotion_status": "RESEARCH_ONLY",
        },
        "model_json": booster.save_raw(raw_format="json").decode(),
    }
    return RegimeModel({**artifact, "fingerprint": _hash(artifact)})


def evaluate_walk_forward(
    datasets: list[dict], *, config: RegimeConfig | None = None, output: str | Path | None = None
) -> dict:
    if not datasets:
        raise ValueError("Walk-forward evaluation requires datasets")
    starts = [datetime.fromisoformat(d["splits"]["validation_end"]) for d in datasets]
    ends = [datetime.fromisoformat(d["splits"]["test_end"]) for d in datasets]
    if any(b < a for a, b in zip(ends, starts[1:])):
        raise ValueError("Walk-forward test windows must not overlap")
    models = [train_regime(dataset, config=config) for dataset in datasets]
    if output is not None:
        for model in models:
            model.save(output)
    return {
        "model_version": MODEL_VERSION,
        "folds": [
            {
                "dataset_fingerprint": m.artifact["dataset_fingerprint"],
                "model_fingerprint": m.artifact["fingerprint"],
                "splits": m.artifact["splits"],
                "test": m.artifact["evaluation"]["test"],
            }
            for m in models
        ],
        "mean_test_macro_f1": sum(m.artifact["evaluation"]["test"]["calibrated"]["macro_f1"] for m in models)
        / len(models),
    }
