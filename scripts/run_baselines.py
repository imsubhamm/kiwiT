from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from kiwit.baselines import donchian_baseline, ema_baseline
from kiwit.persistence import DatasetRecord, LocalResearchStore, sha256_file
from tradingkiwi.nse_data import adjust_splits


data_path = ROOT / "data" / "official_nse" / "niftybees.csv"
actions_path = ROOT / "data" / "reference" / "niftybees_corporate_actions.csv"
raw = pd.read_csv(data_path)
actions = pd.read_csv(actions_path)
data = adjust_splits(raw, actions)
data["date"] = pd.to_datetime(data["date"])
data = data.sort_values("date").set_index("date")

store = LocalResearchStore(ROOT / "data" / "local" / "kiwit_research.sqlite3")
dataset_id = store.register_dataset(DatasetRecord(
    name="NIFTYBEES official NSE bhavcopy 2015-2024",
    source_type="official_exchange",
    source_uri="https://nsearchives.nseindia.com/content/historical/EQUITIES/",
    content_sha256=sha256_file(data_path), row_count=len(raw), retrieved_at=datetime.now(UTC),
    period_start=data.index.min().date(), period_end=data.index.max().date(),
    metadata={"corporate_actions_sha256": sha256_file(actions_path), "adjustment": "back-adjusted 10-for-1 split"},
))

output = ROOT / "output" / "backtests" / "baselines_v1"
output.mkdir(parents=True, exist_ok=True)
for function, strategy_id, version, specification in [
    (ema_baseline, "ema_50_200", "1.0.1-research", {"entry": "after 200-bar warmup, close>EMA200 and EMA50>EMA200", "exit": "close<EMA200 or EMA50<EMA200"}),
    (donchian_baseline, "donchian_50_20", "1.0.0-research", {"entry": "close>prior 50-day high", "exit": "close<prior 20-day low"}),
]:
    store.register_strategy(strategy_id, version, "research", specification)
    started = datetime.now(UTC)
    result = function(data, cost_bps=10)
    completed = datetime.now(UTC)
    code_hash = sha256_file(ROOT / "src" / "kiwit" / "baselines.py")
    try:
        run = store.record_backtest(strategy_id, version, (dataset_id,), code_hash, {"initial_cash": 1_000_000, "cost_bps_each_side": 10}, result.metrics, started, completed)
        run_id = str(run.run_id)
    except ValueError:
        run_id = "existing-immutable-run"
    result.trades.to_csv(output / f"{strategy_id}_trades.csv", index=False)
    result.equity_curve.to_csv(output / f"{strategy_id}_equity.csv")
    (output / f"{strategy_id}_summary.json").write_text(json.dumps({"run_id": run_id, **result.metrics}, indent=2) + "\n")
    print(json.dumps({"run_id": run_id, **result.metrics}, indent=2))
store.close()
