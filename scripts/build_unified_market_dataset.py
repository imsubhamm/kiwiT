from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tradingkiwi.nse_data import adjust_splits


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def check(frame: pd.DataFrame, name: str) -> None:
    required = ["date", "open", "high", "low", "close"]
    if frame.empty or frame["date"].duplicated().any() or frame[required].isna().any().any():
        raise ValueError(f"{name}: empty, duplicate, or required null data")
    if not ((frame["high"] >= frame[["open", "close", "low"]].max(axis=1)) & (frame["low"] <= frame[["open", "close", "high"]].min(axis=1))).all():
        raise ValueError(f"{name}: invalid OHLC")


legacy_equity_path = ROOT / "data" / "official_nse" / "niftybees.csv"
legacy_index_path = ROOT / "data" / "official_nse" / "nifty50.csv"
udiff_equity_path = ROOT / "data" / "market" / "normalized" / "nse" / "niftybees.csv"
udiff_index_path = ROOT / "data" / "market" / "normalized" / "nse" / "nifty50.csv"
actions_path = ROOT / "data" / "reference" / "niftybees_corporate_actions.csv"
output = ROOT / "data" / "market" / "published"
output.mkdir(parents=True, exist_ok=True)

legacy_equity = pd.read_csv(legacy_equity_path)
legacy_equity["source_sha256"] = digest(legacy_equity_path)
legacy_equity["source_format"] = "nse_cm_legacy_selected"
legacy_equity["isin"] = ""
legacy_equity = legacy_equity.rename(columns={"date": "trading_date"})
legacy_equity["symbol"] = "NIFTYBEES"
legacy_equity["series"] = "EQ"
new_equity = pd.read_csv(udiff_equity_path)

columns = ["trading_date", "symbol", "series", "open", "high", "low", "close", "volume", "source_sha256", "source_format", "isin"]
equity = pd.concat([legacy_equity[columns], new_equity[columns]], ignore_index=True)
equity["trading_date"] = pd.to_datetime(equity["trading_date"])
equity = equity.sort_values("trading_date").drop_duplicates("trading_date", keep="last")

legacy_index = pd.read_csv(legacy_index_path).rename(columns={"date": "trading_date"})
legacy_index["symbol"] = "NIFTY 50"
legacy_index["series"] = "INDEX"
legacy_index["volume"] = 0
legacy_index["source_sha256"] = digest(legacy_index_path)
legacy_index["source_format"] = "nse_index_legacy_selected"
legacy_index["isin"] = ""
new_index = pd.read_csv(udiff_index_path)
index = pd.concat([legacy_index[columns], new_index[columns]], ignore_index=True)
index["trading_date"] = pd.to_datetime(index["trading_date"])
index = index.sort_values("trading_date").drop_duplicates("trading_date", keep="last")

matched = sorted(set(equity.trading_date) & set(index.trading_date))
equity = equity[equity.trading_date.isin(matched)].copy()
index = index[index.trading_date.isin(matched)].copy()
if not equity.trading_date.reset_index(drop=True).equals(index.trading_date.reset_index(drop=True)):
    raise ValueError("equity/index dates do not align")

execution = equity.rename(columns={"trading_date": "date"})
index_output = index.rename(columns={"trading_date": "date"})
check(execution, "NIFTYBEES execution")
check(index_output, "NIFTY 50")
adjusted = adjust_splits(execution, pd.read_csv(actions_path))
check(adjusted, "NIFTYBEES adjusted")

execution_path = output / "niftybees_execution_unadjusted.csv"
adjusted_path = output / "niftybees_research_adjusted.csv"
index_path = output / "nifty50.csv"
execution.to_csv(execution_path, index=False, date_format="%Y-%m-%d")
adjusted.to_csv(adjusted_path, index=False, date_format="%Y-%m-%d")
index_output.to_csv(index_path, index=False, date_format="%Y-%m-%d")

manifest = {
    "status": "published",
    "matched_rows": len(matched),
    "period_start": str(min(matched).date()),
    "period_end": str(max(matched).date()),
    "outputs": {
        path.name: {"sha256": digest(path), "rows": sum(1 for _ in path.open()) - 1}
        for path in (execution_path, adjusted_path, index_path)
    },
    "inputs": {
        "legacy_niftybees": digest(legacy_equity_path),
        "legacy_nifty50": digest(legacy_index_path),
        "udiff_niftybees": digest(udiff_equity_path),
        "udiff_nifty50": digest(udiff_index_path),
        "niftybees_corporate_actions": digest(actions_path),
    },
}
(output / "dataset_manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
print(json.dumps(manifest, indent=2))
