from __future__ import annotations

import json
import math
import sys
from dataclasses import asdict
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from kiwit.baselines import donchian_baseline, ema_baseline
from kiwit.research import bootstrap_trades, cost_stress, fixed_walk_forward, parameter_neighborhood, passive_benchmark, promotion_gates, randomized_entry_control


data = pd.read_csv(ROOT / "data" / "market" / "published" / "niftybees_research_adjusted.csv", parse_dates=["date"])
data = data.sort_values("date").set_index("date")
output = ROOT / "output" / "research" / "strategy_validation_v1"
output.mkdir(parents=True, exist_ok=True)


def json_safe(value):
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {key: json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [json_safe(item) for item in value]
    return value

passive = passive_benchmark(data)
suite = {"dataset": {"rows": len(data), "start": str(data.index.min().date()), "end": str(data.index.max().date())}, "passive": passive.metrics, "strategies": {}}

for name, runner, strategy, parameters in [
    ("ema_50_200", ema_baseline, "ema", {"fast_span": 50, "slow_span": 200}),
    ("donchian_50_20", donchian_baseline, "donchian", {"entry_window": 50, "exit_window": 20}),
]:
    result = runner(data, 1_000_000, 10)
    folds = fixed_walk_forward(data, runner)
    random_control = randomized_entry_control(data, strategy, parameters)
    neighborhood = parameter_neighborhood(data, strategy)
    bootstrap = bootstrap_trades(result.trades)
    gates = promotion_gates(result, folds, random_control, neighborhood)
    payload = {
        "primary": result.metrics, "walk_forward": folds, "cost_stress": cost_stress(data, runner),
        "randomized_entry_control": random_control, "parameter_neighborhood": neighborhood,
        "bootstrap": bootstrap, "promotion_gates": [asdict(gate) for gate in gates],
        "promoted": all(gate.passed for gate in gates),
    }
    suite["strategies"][name] = payload
    result.trades.to_csv(output / f"{name}_trades.csv", index=False)

safe_suite = json_safe(suite)
(output / "validation_suite.json").write_text(json.dumps(safe_suite, indent=2, allow_nan=False) + "\n")
print(json.dumps(safe_suite, indent=2, allow_nan=False))
