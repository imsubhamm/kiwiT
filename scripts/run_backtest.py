from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tradingkiwi.backtest import prepare, run_backtest
from tradingkiwi.nse_data import adjust_splits


data_dir = ROOT / "data" / "official_nse"
output_dir = ROOT / "output" / "backtests" / "strategy_a_v0"
output_dir.mkdir(parents=True, exist_ok=True)

equity = adjust_splits(
    pd.read_csv(data_dir / "niftybees.csv"),
    pd.read_csv(ROOT / "data" / "reference" / "niftybees_corporate_actions.csv"),
)
index = pd.read_csv(data_dir / "nifty50.csv")
prepared = prepare(equity, index)
trades, curve, summary = run_backtest(prepared)
trades.to_csv(output_dir / "trades.csv", index=False)
curve.to_csv(output_dir / "equity_curve.csv")
(output_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
print(json.dumps(summary, indent=2))
