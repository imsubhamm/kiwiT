"""Trend opportunity scoring and chronological cost-aware research evaluation."""

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
from .regime import FEATURES, RegimeConfig, _runtime, _temperature, _validate

MODEL_VERSION = "trend-xgb-v1"


@dataclass(frozen=True)
class TrendConfig(RegimeConfig):
    round_trip_cost: float = 0.0002
    round_trip_slippage: float = 0.0004

    def __post_init__(self):
        super().__post_init__()
        for value in (self.round_trip_cost, self.round_trip_slippage):
            if not math.isfinite(value) or not 0 <= value < 1:
                raise ValueError("Costs and slippage must be finite nonnegative return fractions")
        if self.round_trip_cost + self.round_trip_slippage >= 1:
            raise ValueError("Combined cost/slippage must be below one")


def _accepted(p, config):
    return (p >= config.minimum_confidence) & (2 * p - 1 >= config.minimum_margin)


def _binary_metrics(y, p, config):
    np, _ = _runtime()
    pred = p >= 0.5
    tp, fp = int(((y == 1) & pred).sum()), int(((y == 0) & pred).sum())
    fn, tn = int(((y == 1) & ~pred).sum()), int(((y == 0) & ~pred).sum())
    accepted = _accepted(p, config)
    bins, ece = [], 0.0
    for i in range(10):
        mask = np.minimum((p * 10).astype(int), 9) == i
        count = int(mask.sum())
        mean = float(p[mask].mean()) if count else None
        actual = float(y[mask].mean()) if count else None
        if count:
            ece += count / len(y) * abs(mean - actual)
        bins.append(
            {
                "lower": i / 10,
                "upper": (i + 1) / 10,
                "count": count,
                "predicted_success": mean,
                "observed_success": actual,
            }
        )
    return {
        "rows": len(y),
        "class_counts": {"failure": tn + fp, "success": tp + fn},
        "accuracy": (tp + tn) / len(y),
        "f1": 2 * tp / (2 * tp + fp + fn) if tp + fp + fn else 0,
        "log_loss": float(-(y * np.log(np.clip(p, 1e-15, 1)) + (1 - y) * np.log(np.clip(1 - p, 1e-15, 1))).mean()),
        "brier_score": float(((p - y) ** 2).mean()),
        "expected_calibration_error": ece,
        "reliability_bins": bins,
        "confusion_matrix": [[tn, fp], [fn, tp]],
        "signal_coverage": float(accepted.mean()),
        "selected_success_rate": float(y[accepted].mean()) if accepted.any() else None,
    }


def _trade_summary(trades):
    count = len(trades)
    return {
        "trades": count,
        "gross_return_sum": sum(t["gross_return"] for t in trades),
        "net_return_sum": sum(t["net_return"] for t in trades),
        "mean_net_return": sum(t["net_return"] for t in trades) / count if count else None,
        "win_rate_after_costs": sum(t["net_return"] > 0 for t in trades) / count if count else None,
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
        gross = float(row["labels"]["forward_return"]) * row["labels"]["trend_side"]
        next_entry = datetime.fromisoformat(row["label_end"])
        trades.append(
            {
                "at": row["at"],
                "exit_at": row["label_end"],
                "day": at.astimezone(IST).date().isoformat(),
                "regime": row["labels"]["regime"],
                "direction": "LONG" if row["labels"]["trend_side"] == 1 else "SHORT",
                "gross_return": gross,
                "net_return": gross - config.round_trip_cost - config.round_trip_slippage,
            }
        )
    return {**_trade_summary(trades), "overlap_signals_skipped": skipped, "trades_detail": trades}


def _evaluate(rows, probabilities, config):
    np, _ = _runtime()
    y = np.asarray([r["labels"]["trend_success"] for r in rows])
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
                "model_strategy": _trade_summary([t for t in model_trades["trades_detail"] if t[category] == key]),
                "rule_strategy": _trade_summary([t for t in rule_trades["trades_detail"] if t[category] == key]),
            }
    return {
        "classification": _binary_metrics(y, probabilities, config),
        "deterministic_rule_classification": _binary_metrics(y, rule.astype(float), config),
        "strategy": model_trades,
        "deterministic_rule_strategy": rule_trades,
        "by": groups,
    }


class TrendModel:
    def __init__(self, artifact: dict):
        body = dict(artifact)
        fingerprint = body.pop("fingerprint")
        if fingerprint != _hash(body) or artifact["model_version"] != MODEL_VERSION:
            raise ValueError("Trend artifact fingerprint/version mismatch")
        if tuple(artifact["features"]) != FEATURES or artifact["feature_version"] != FEATURE_VERSION:
            raise ValueError("Trend feature contract mismatch")
        if not 0 < artifact["temperature"] < 100:
            raise ValueError("Invalid calibration temperature")
        _, xgb = _runtime()
        self.config = TrendConfig(**artifact["config"])
        self.artifact = json.loads(_json(artifact))
        self._booster = xgb.Booster(params={"nthread": 1, "device": "cpu"})
        self._booster.load_model(bytearray(artifact["model_json"].encode()))

    def predict(self, snapshot: FeatureSnapshot) -> dict:
        result = {
            "direction": "NONE",
            "opportunity_probability": None,
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
        spread = snapshot.values["ema_spread"]
        if spread == 0:
            return {**result, "reason_codes": ["NO_TREND_DIRECTION"]}
        np, xgb = _runtime()
        p = self._booster.predict(xgb.DMatrix(np.asarray([values]), feature_names=list(FEATURES), nthread=1))
        calibrated = _temperature(np.column_stack((1 - p, p)), self.artifact["temperature"])[0, 1]
        accepted = bool(_accepted(calibrated, self.config))
        return {
            **result,
            "opportunity_probability": float(calibrated),
            "direction": ("LONG" if spread > 0 else "SHORT") if accepted else "NONE",
            "reason_codes": ["TREND_OPPORTUNITY"] if accepted else ["LOW_OPPORTUNITY_CONFIDENCE"],
        }

    def save(self, directory: str | Path) -> Path:
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"trend-{self.artifact['fingerprint']}.json"
        body = dict(self.artifact)
        fingerprint = body.pop("fingerprint")
        if _hash(body) != fingerprint:
            raise ValueError("Trend artifact mutated after training")
        content = _json(self.artifact) + "\n"
        try:
            with path.open("x", encoding="utf-8") as stream:
                stream.write(content)
        except FileExistsError:
            if path.read_text() != content:
                raise ValueError("Existing trend artifact differs") from None
        return path

    @classmethod
    def load(cls, path: str | Path):
        return cls(json.loads(Path(path).read_text()))


def train_trend(dataset: dict, *, config: TrendConfig | None = None) -> TrendModel:
    config = config or TrendConfig()
    all_groups = _validate(dataset, require_all_regimes=False)
    groups, excluded = {}, {}
    for split, rows in all_groups.items():
        groups[split] = []
        for row in rows:
            spread = row["features"]["values"]["ema_spread"]
            side = 1 if spread > 0 else -1 if spread < 0 else 0
            if row["labels"]["trend_side"] != side:
                raise ValueError("Trend label direction differs from contemporaneous EMA direction")
            label = row["labels"]["trend_success"]
            if side == 0:
                if label is not None:
                    raise ValueError("Ineligible trend must have null target")
                continue
            if type(label) is not int or label not in {0, 1}:
                raise ValueError("Trend target must be binary")
            forward = float(row["labels"]["forward_return"])
            if not math.isfinite(forward):
                raise ValueError("Nonfinite forward return")
            groups[split].append(row)
        excluded[split] = len(rows) - len(groups[split])
    if any(not rows for rows in groups.values()):
        raise ValueError("Every split needs eligible trend samples")
    if {r["labels"]["trend_success"] for r in groups["train"]} != {0, 1}:
        raise ValueError("Trend training requires successes and failures")
    np, xgb = _runtime()
    matrices = {
        name: xgb.DMatrix(
            np.asarray([[r["features"]["values"][f] for f in FEATURES] for r in rows]),
            label=[r["labels"]["trend_success"] for r in rows],
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
    validation_y = np.asarray([r["labels"]["trend_success"] for r in groups["validation"]])

    def calibrate(p, t):
        return _temperature(np.column_stack((1 - p, p)), t)[:, 1]

    temperature = min(
        (0.5, 0.75, 1.0, 1.5, 2.0, 3.0),
        key=lambda t: (_binary_metrics(validation_y, calibrate(raw["validation"], t), config)["log_loss"], abs(t - 1)),
    )
    evaluation = {
        name: {
            "raw_classification": _binary_metrics(
                np.asarray([r["labels"]["trend_success"] for r in rows]), raw[name], config
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
        "training_class_counts": dict(Counter(str(r["labels"]["trend_success"]) for r in groups["train"])),
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
    return TrendModel({**artifact, "fingerprint": _hash(artifact)})
