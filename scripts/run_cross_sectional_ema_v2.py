from __future__ import annotations

import csv
import json
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "market" / "raw" / "nse" / "cm"
INDEX = ROOT / "data" / "market" / "published" / "nifty50.csv"
OUTPUT = ROOT / "output" / "research" / "cross_sectional_ema_v2"


def load_equities() -> pd.DataFrame:
    records: list[dict] = []
    paths = sorted(RAW.rglob("BhavCopy_NSE_CM_*.csv.zip")) + sorted(RAW.rglob("cm*bhav.csv.zip"))
    for path in paths:
        with zipfile.ZipFile(path) as archive:
            name = next(name for name in archive.namelist() if name.lower().endswith(".csv"))
            with archive.open(name) as raw:
                rows = csv.DictReader(line.decode("utf-8-sig") for line in raw)
                for row in rows:
                    legacy = "SYMBOL" in row
                    series = row.get("SERIES" if legacy else "SctySrs", "").strip()
                    if series != "EQ":
                        continue
                    records.append({
                        "date": row["TIMESTAMP" if legacy else "TradDt"],
                        "symbol": row["SYMBOL" if legacy else "TckrSymb"].strip(),
                        "open": float(row["OPEN" if legacy else "OpnPric"]),
                        "close": float(row["CLOSE" if legacy else "ClsPric"]),
                        "volume": float(row["TOTTRDQTY" if legacy else "TtlTradgVol"]),
                    })
    frame = pd.DataFrame.from_records(records)
    if frame.empty:
        raise RuntimeError("no UDiFF EQ records found")
    frame["date"] = pd.to_datetime(frame["date"], format="mixed", dayfirst=True)
    frame = frame.drop_duplicates(["date", "symbol"]).sort_values(["symbol", "date"])
    return frame[(frame.open > 0) & (frame.close > 0) & (frame.volume >= 0)].copy()


def signals(frame: pd.DataFrame, fast: int = 20, slow: int = 100) -> pd.DataFrame:
    grouped = frame.groupby("symbol", group_keys=False)
    frame["ema_fast"] = grouped.close.transform(lambda values: values.ewm(span=fast, adjust=False).mean())
    frame["ema_slow"] = grouped.close.transform(lambda values: values.ewm(span=slow, adjust=False).mean())
    frame["history"] = grouped.cumcount() + 1
    frame["turnover"] = frame.close * frame.volume
    frame["median_turnover"] = grouped.turnover.transform(lambda values: values.rolling(60, min_periods=40).median().shift(1))
    frame["liquidity_rank"] = frame.groupby("date").median_turnover.rank(method="first", ascending=False)
    frame["previous_close"] = grouped.close.shift(1)
    frame["previous_fast"] = grouped.ema_fast.shift(1)
    frame["previous_slow"] = grouped.ema_slow.shift(1)
    frame["event_jump"] = (frame.close / frame.previous_close - 1).abs() > 0.30
    frame["eligible"] = (frame.history >= 120) & (frame.liquidity_rank <= 250) & (frame.previous_close >= 20)
    frame["entry"] = frame.eligible & (frame.ema_fast > frame.ema_slow) & (frame.previous_fast <= frame.previous_slow)
    frame["exit"] = (frame.ema_fast < frame.ema_slow) & (frame.previous_fast >= frame.previous_slow)
    return frame


def trade_list(frame: pd.DataFrame, fast: int, slow: int, cost_bps: float) -> pd.DataFrame:
    data = signals(frame.copy(), fast, slow)
    index = pd.read_csv(INDEX, parse_dates=["date"]).sort_values("date")
    index["regime"] = index.close > index.close.ewm(span=200, adjust=False).mean()
    regime = index.set_index("date").regime
    data["regime"] = data.date.map(regime).fillna(False)
    trades: list[dict] = []
    for symbol, rows in data.groupby("symbol"):
        values = list(rows.sort_values("date").itertuples(index=False))
        open_trade = None
        contaminated = False
        for i, row in enumerate(values):
            if open_trade is not None:
                contaminated = contaminated or bool(row.event_jump)
                if row.exit and i + 1 < len(values):
                    nxt = values[i + 1]
                    gross = float(nxt.open / open_trade["entry_price"] - 1)
                    net = gross - 2 * cost_bps / 10000
                    trades.append({**open_trade, "exit_date": str(nxt.date.date()), "exit_price": float(nxt.open),
                                   "gross_return": gross, "net_return": net, "event_contaminated": contaminated})
                    open_trade, contaminated = None, False
            elif row.entry and row.regime and i + 1 < len(values):
                nxt = values[i + 1]
                open_trade = {"symbol": symbol, "signal_date": str(row.date.date()),
                              "entry_date": str(nxt.date.date()), "entry_price": float(nxt.open)}
        if open_trade is not None and values:
            last = values[-1]
            gross = float(last.close / open_trade["entry_price"] - 1)
            trades.append({**open_trade, "exit_date": str(last.date.date()), "exit_price": float(last.close),
                           "gross_return": gross, "net_return": gross - 2 * cost_bps / 10000,
                           "event_contaminated": contaminated, "forced_final_exit": True})
    return pd.DataFrame(trades)


def metrics(trades: pd.DataFrame) -> dict:
    clean = trades[~trades.event_contaminated].copy() if not trades.empty else trades
    if clean.empty:
        return {"trades": 0, "total_return": 0, "profit_factor": 0, "median_trade": 0, "max_drawdown": 0}
    clean["entry_date"] = pd.to_datetime(clean.entry_date)
    clean["exit_date"] = pd.to_datetime(clean.exit_date)
    accepted = []
    active_exits: list[pd.Timestamp] = []
    for row in clean.sort_values(["entry_date", "symbol"]).itertuples(index=False):
        active_exits = [exit_date for exit_date in active_exits if exit_date >= row.entry_date]
        if len(active_exits) >= 20:
            continue
        accepted.append(row)
        active_exits.append(row.exit_date)
    clean = pd.DataFrame(accepted)
    returns = clean.net_return.clip(lower=-0.99)
    daily_returns = clean.assign(slot_return=returns / 20).groupby("exit_date").slot_return.sum().sort_index()
    curve = (1 + daily_returns).cumprod()
    drawdown = curve / curve.cummax() - 1
    gains, losses = returns[returns > 0].sum(), -returns[returns < 0].sum()
    return {"trades": len(clean), "capacity_rejected": int(len(trades) - int(trades.event_contaminated.sum()) - len(clean)),
            "excluded_corporate_events": int(trades.event_contaminated.sum()),
            "total_return": float(curve.iloc[-1] - 1), "profit_factor": float(gains / losses) if losses else None,
            "median_trade": float(returns.median()), "win_rate": float((returns > 0).mean()),
            "max_drawdown": float(drawdown.min())}


def main() -> None:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    raw = load_equities()
    primary_trades = trade_list(raw, 20, 100, 10)
    primary = metrics(primary_trades)
    costs = {str(cost): metrics(trade_list(raw, 20, 100, cost)) for cost in (0, 5, 10, 20, 30)}
    neighbors = []
    for fast in (15, 20, 25):
        for slow in (80, 100, 120):
            result = metrics(trade_list(raw, fast, slow, 10))
            neighbors.append({"fast": fast, "slow": slow, **result})
    clean = primary_trades[~primary_trades.event_contaminated].sort_values("entry_date")
    folds = [metrics(chunk) for chunk in np.array_split(clean, 4) if len(chunk)]
    gates = {
        "minimum_200_trades": primary["trades"] >= 200,
        "profit_factor_above_1_20": (primary["profit_factor"] or 0) > 1.20,
        "positive_median_trade": primary["median_trade"] > 0,
        "three_of_four_folds_positive": sum(fold["total_return"] > 0 for fold in folds) >= 3,
        "max_drawdown_no_worse_than_15pct": primary["max_drawdown"] >= -0.15,
        "positive_at_30_bps": costs["30"]["total_return"] > 0,
        "seven_of_nine_neighbors_profitable": sum(item["total_return"] > 0 for item in neighbors) >= 7,
        "random_control_95th_percentile": False,
    }
    result = {"specification": "STRATEGY_EMA_CROSS_SECTIONAL_V2_SPEC.md", "dataset": {
        "rows": len(raw), "symbols": raw.symbol.nunique(), "start": str(raw.date.min().date()), "end": str(raw.date.max().date())},
        "primary": primary, "cost_stress": costs, "folds": folds, "parameter_neighbors": neighbors,
        "promotion_gates": gates, "promoted": all(gates.values()),
        "limitations": ["Randomized-entry control remains required before promotion."],
    }
    primary_trades.to_csv(OUTPUT / "trades.csv", index=False)
    (OUTPUT / "validation.json").write_text(json.dumps(result, indent=2, allow_nan=False) + "\n")
    print(json.dumps(result, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
