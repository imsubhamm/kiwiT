from __future__ import annotations

import csv
import hashlib
import json
import zipfile
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "market" / "raw" / "nse" / "cm"
INDEX = ROOT / "data" / "market" / "published" / "nifty50.csv"
OUTPUT = ROOT / "output" / "research" / "regime_router_v1"
SPECIFICATION = ROOT / "research" / "STRATEGY_REGIME_ROUTER_V1_SPEC.md"


@dataclass(frozen=True)
class Parameters:
    bull_breakout: int = 20
    bull_exit: int = 10
    range_rsi: float = 10
    range_max_hold: int = 7


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_equities() -> pd.DataFrame:
    records: list[dict] = []
    paths = sorted(RAW.rglob("BhavCopy_NSE_CM_*.csv.zip")) + sorted(RAW.rglob("cm*bhav.csv.zip"))
    for path in paths:
        with zipfile.ZipFile(path) as archive:
            name = next(name for name in archive.namelist() if name.lower().endswith(".csv"))
            with archive.open(name) as raw:
                for row in csv.DictReader(line.decode("utf-8-sig") for line in raw):
                    legacy = "SYMBOL" in row
                    if row.get("SERIES" if legacy else "SctySrs", "").strip() != "EQ":
                        continue
                    records.append({
                        "date": row["TIMESTAMP" if legacy else "TradDt"],
                        "symbol": row["SYMBOL" if legacy else "TckrSymb"].strip(),
                        "open": float(row["OPEN" if legacy else "OpnPric"]),
                        "high": float(row["HIGH" if legacy else "HghPric"]),
                        "low": float(row["LOW" if legacy else "LwPric"]),
                        "close": float(row["CLOSE" if legacy else "ClsPric"]),
                        "volume": float(row["TOTTRDQTY" if legacy else "TtlTradgVol"]),
                    })
    frame = pd.DataFrame.from_records(records)
    if frame.empty:
        raise RuntimeError("no official NSE EQ bhavcopy rows found")
    frame["date"] = pd.to_datetime(frame.date, format="mixed", dayfirst=True)
    frame = frame.drop_duplicates(["date", "symbol"], keep="last").sort_values(["symbol", "date"])
    valid = (
        (frame.open > 0) & (frame.high > 0) & (frame.low > 0) & (frame.close > 0) & (frame.volume >= 0)
        & (frame.high >= frame[["open", "close", "low"]].max(axis=1))
        & (frame.low <= frame[["open", "close", "high"]].min(axis=1))
    )
    return frame[valid].copy()


def add_features(frame: pd.DataFrame, parameters: Parameters) -> pd.DataFrame:
    grouped = frame.groupby("symbol", group_keys=False)
    frame["history"] = grouped.cumcount() + 1
    frame["previous_close"] = grouped.close.shift(1)
    frame["event_jump"] = (frame.close / frame.previous_close - 1).abs() > 0.30
    frame["turnover"] = frame.close * frame.volume
    frame["median_turnover"] = grouped.turnover.transform(
        lambda values: values.rolling(60, min_periods=40).median().shift(1)
    )
    frame["liquidity_rank"] = frame.groupby("date").median_turnover.rank(method="first", ascending=False)
    frame["sma5"] = grouped.close.transform(lambda values: values.rolling(5, min_periods=5).mean())
    frame["sma50"] = grouped.close.transform(lambda values: values.rolling(50, min_periods=50).mean())
    frame["sma200"] = grouped.close.transform(lambda values: values.rolling(200, min_periods=200).mean())
    frame["prior_bull_high"] = grouped.high.transform(
        lambda values: values.rolling(parameters.bull_breakout, min_periods=parameters.bull_breakout).max().shift(1)
    )
    frame["prior_bull_low"] = grouped.low.transform(
        lambda values: values.rolling(parameters.bull_exit, min_periods=parameters.bull_exit).min().shift(1)
    )
    frame["return20"] = grouped.close.pct_change(20)
    change = grouped.close.diff()
    gain = change.clip(lower=0).groupby(frame.symbol).transform(lambda values: values.ewm(alpha=0.5, adjust=False).mean())
    loss = (-change.clip(upper=0)).groupby(frame.symbol).transform(
        lambda values: values.ewm(alpha=0.5, adjust=False).mean()
    )
    rs = gain / loss.replace(0, np.nan)
    frame["rsi2"] = (100 - 100 / (1 + rs)).fillna(100)
    frame["eligible"] = (frame.history >= 220) & (frame.liquidity_rank <= 250) & (frame.previous_close >= 20)
    frame["bull_entry"] = frame.eligible & (frame.close > frame.prior_bull_high) & (frame.close > frame.sma50)
    frame["range_entry"] = (
        frame.eligible & (frame.rsi2 < parameters.range_rsi) & (frame.close < frame.sma5) & (frame.close > frame.sma200)
    )
    return frame.sort_values(["date", "symbol"])


def regime_series() -> pd.Series:
    index = pd.read_csv(INDEX, parse_dates=["date"]).sort_values("date")
    index["ema50"] = index.close.ewm(span=50, adjust=False).mean()
    index["ema200"] = index.close.ewm(span=200, adjust=False).mean()
    index["return20"] = index.close.pct_change(20)
    index["regime"] = np.where(
        (index.close > index.ema200) & (index.ema50 > index.ema200) & (index.return20 > 0.02),
        "bull_trend",
        np.where(index.close > index.ema200, "range", "risk_off"),
    )
    return index.set_index("date").regime


def simulate(
    featured: pd.DataFrame,
    regimes: pd.Series,
    parameters: Parameters,
    cost_bps: float = 10,
    *,
    random_seed: int | None = None,
    rows_by_date: dict | None = None,
) -> dict:
    rng = np.random.default_rng(random_seed)
    dates = sorted(featured.date.unique())
    if rows_by_date is None:
        rows_by_date = daily_views(featured)
    cash = 1_000_000.0
    positions: dict[str, dict] = {}
    pending_entries: list[tuple[str, str]] = []
    pending_exits: set[str] = set()
    trades: list[dict] = []
    curve: list[dict] = []
    last_marks: dict[str, float] = {}
    entry_cost = cost_bps / 10000

    for date in dates:
        day = rows_by_date[date]
        for symbol in sorted(pending_exits):
            if symbol not in positions or symbol not in day.index:
                continue
            row = day.loc[symbol]
            position = positions.pop(symbol)
            exit_price = float(row.open) * (1 - entry_cost)
            cash += position["quantity"] * exit_price
            gross = float(row.open) / position["raw_entry"] - 1
            net = exit_price / position["entry_price"] - 1
            trades.append({
                "symbol": symbol, "child": position["child"], "entry_date": str(position["entry_date"].date()),
                "exit_date": str(pd.Timestamp(date).date()), "gross_return": gross, "net_return": net,
                "holding_sessions": position["held"], "event_contaminated": position["contaminated"],
            })
        pending_exits.clear()

        marked_before_entries = cash + sum(
            position["quantity"] * last_marks.get(symbol, position["raw_entry"])
            for symbol, position in positions.items()
        )
        allocation = marked_before_entries / 20
        for symbol, child in pending_entries:
            if len(positions) >= 20 or symbol in positions or symbol not in day.index:
                continue
            row = day.loc[symbol]
            raw_entry = float(row.open)
            entry_price = raw_entry * (1 + entry_cost)
            quantity = int(min(allocation, cash) // entry_price)
            if quantity < 1:
                continue
            cash -= quantity * entry_price
            positions[symbol] = {
                "child": child, "quantity": quantity, "raw_entry": raw_entry, "entry_price": entry_price,
                "entry_date": pd.Timestamp(date), "held": 0, "contaminated": False,
            }
        pending_entries = []

        for symbol, position in positions.items():
            if symbol not in day.index:
                continue
            row = day.loc[symbol]
            last_marks[symbol] = float(row.close)
            position["held"] += 1
            position["contaminated"] = position["contaminated"] or bool(row.event_jump)
            close_loss = float(row.close) / position["raw_entry"] - 1
            if position["child"] == "bull_breakout":
                should_exit = (
                    float(row.close) < float(row.prior_bull_low) or close_loss <= -0.08 or position["held"] >= 40
                )
            else:
                should_exit = float(row.close) > float(row.sma5) or close_loss <= -0.05 or position["held"] >= parameters.range_max_hold
            if should_exit:
                pending_exits.add(symbol)

        equity = cash + sum(
            position["quantity"] * last_marks.get(symbol, position["raw_entry"])
            for symbol, position in positions.items()
        )
        curve.append({"date": str(pd.Timestamp(date).date()), "equity": equity, "cash": cash, "positions": len(positions)})

        regime = str(regimes.get(date, "risk_off"))
        if regime == "risk_off":
            continue
        signal_column = "bull_entry" if regime == "bull_trend" else "range_entry"
        child = "bull_breakout" if regime == "bull_trend" else "range_pullback"
        candidates = day[day[signal_column] & ~day.symbol.isin(positions) & ~day.symbol.isin(pending_exits)].copy()
        if random_seed is not None:
            count = len(candidates)
            pool = day[day.eligible & ~day.symbol.isin(positions) & ~day.symbol.isin(pending_exits)].copy()
            if count and len(pool):
                selected = rng.choice(pool.symbol.to_numpy(), size=min(count, len(pool)), replace=False)
                candidates = pool[pool.symbol.isin(selected)]
            else:
                candidates = pool.iloc[0:0]
        order = ["return20", "symbol"] if child == "bull_breakout" else ["rsi2", "symbol"]
        ascending = [False, True] if child == "bull_breakout" else [True, True]
        pending_entries = [(symbol, child) for symbol in candidates.sort_values(order, ascending=ascending).symbol]

    terminal_date = pd.Timestamp(dates[-1])
    for symbol, position in positions.items():
        mark = last_marks.get(symbol, position["raw_entry"])
        exit_price = mark * (1 - entry_cost)
        cash += position["quantity"] * exit_price
        trades.append({
            "symbol": symbol, "child": position["child"], "entry_date": str(position["entry_date"].date()),
            "exit_date": str(terminal_date.date()), "gross_return": mark / position["raw_entry"] - 1,
            "net_return": exit_price / position["entry_price"] - 1, "holding_sessions": position["held"],
            "event_contaminated": position["contaminated"], "forced_final_exit": True,
        })
    if curve:
        curve[-1]["equity"] = cash
        curve[-1]["cash"] = cash
        curve[-1]["positions"] = 0
    return {"trades": pd.DataFrame(trades), "curve": pd.DataFrame(curve)}


def daily_views(featured: pd.DataFrame) -> dict:
    views = {}
    for date, rows in featured.groupby("date", sort=True):
        indexed = rows.set_index("symbol", drop=False)
        indexed.index.name = None
        views[date] = indexed
    return views


def summarize(result: dict) -> dict:
    trades = result["trades"]
    curve = result["curve"]
    clean = trades[~trades.event_contaminated].copy() if len(trades) else trades
    returns = clean.net_return if len(clean) else pd.Series(dtype=float)
    equity = curve.equity
    drawdown = equity / equity.cummax() - 1
    daily = equity.pct_change().fillna(0)
    gains = returns[returns > 0].sum()
    losses = -returns[returns < 0].sum()
    return {
        "trades": len(clean),
        "excluded_corporate_events": int(len(trades) - len(clean)),
        "total_return": float(equity.iloc[-1] / 1_000_000 - 1),
        "profit_factor": float(gains / losses) if losses else None,
        "median_trade": float(returns.median()) if len(returns) else 0,
        "win_rate": float((returns > 0).mean()) if len(returns) else 0,
        "max_drawdown": float(drawdown.min()),
        "daily_sharpe": float(np.sqrt(252) * daily.mean() / daily.std()) if daily.std() else 0,
        "bull_trades": int((clean.child == "bull_breakout").sum()) if len(clean) else 0,
        "range_trades": int((clean.child == "range_pullback").sum()) if len(clean) else 0,
    }


def fold_returns(curve: pd.DataFrame) -> list[dict]:
    folds = []
    for index, chunk in enumerate(np.array_split(curve, 4), start=1):
        start, end = float(chunk.equity.iloc[0]), float(chunk.equity.iloc[-1])
        folds.append({"fold": index, "start": chunk.date.iloc[0], "end": chunk.date.iloc[-1], "return": end / start - 1})
    return folds


def main() -> None:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    raw = load_equities()
    regimes = regime_series()
    primary_parameters = Parameters()
    featured = add_features(raw.copy(), primary_parameters)
    primary_views = daily_views(featured)
    primary_result = simulate(featured, regimes, primary_parameters, rows_by_date=primary_views)
    primary = summarize(primary_result)
    costs = {
        str(cost): summarize(simulate(featured, regimes, primary_parameters, cost, rows_by_date=primary_views))
        for cost in (0, 5, 20, 30)
    }
    neighbor_parameters = [
        Parameters(bull, bull_exit, rsi, hold)
        for bull, bull_exit, rsi, hold in (
            (15, 8, 8, 5), (15, 10, 10, 7), (15, 12, 12, 9),
            (20, 8, 10, 9), (20, 10, 10, 7), (20, 12, 8, 5),
            (25, 8, 12, 7), (25, 10, 8, 9), (25, 12, 10, 5),
        )
    ]
    neighbors = []
    for parameters in neighbor_parameters:
        neighbor_featured = add_features(raw.copy(), parameters)
        neighbor_views = daily_views(neighbor_featured)
        neighbors.append({
            **parameters.__dict__,
            **summarize(simulate(neighbor_featured, regimes, parameters, rows_by_date=neighbor_views)),
        })
    random_returns = []
    for seed in range(200):
        random_returns.append(summarize(
            simulate(featured, regimes, primary_parameters, random_seed=seed, rows_by_date=primary_views)
        )["total_return"])
    actual_percentile = float((np.asarray(random_returns) <= primary["total_return"]).mean() * 100)
    folds = fold_returns(primary_result["curve"])
    gates = {
        "point_in_time_universe": True,
        "minimum_200_trades": primary["trades"] >= 200,
        "profit_factor_above_1_20": (primary["profit_factor"] or 0) > 1.20,
        "median_trade_after_costs": primary["median_trade"] > 0,
        "walk_forward_majority_positive": sum(fold["return"] > 0 for fold in folds) >= 3,
        "maximum_drawdown": primary["max_drawdown"] >= -0.15,
        "cost_stress": costs["30"]["total_return"] > 0,
        "parameter_neighborhood": sum(item["total_return"] > 0 for item in neighbors) >= 7,
        "random_entry_percentile": actual_percentile >= 95,
    }
    validation = {
        "strategy_id": "regime_router", "version": "1.0.0", "specification": SPECIFICATION.name,
        "specification_sha256": sha256(SPECIFICATION),
        "dataset": {"rows": len(raw), "symbols": raw.symbol.nunique(), "start": str(raw.date.min().date()),
                    "end": str(raw.date.max().date())},
        "primary": primary, "cost_stress": costs, "walk_forward": folds, "parameter_neighbors": neighbors,
        "randomized_entry_control": {
            "simulations": 200, "seed_range": [0, 199], "actual_percentile": actual_percentile,
            "median_return": float(np.median(random_returns)), "p95_return": float(np.percentile(random_returns, 95)),
        },
        "promotion_gates": gates, "promoted": all(gates.values()),
    }
    primary_result["trades"].to_csv(OUTPUT / "trades.csv", index=False)
    primary_result["curve"].to_csv(OUTPUT / "equity_curve.csv", index=False)
    (OUTPUT / "validation.json").write_text(json.dumps(validation, indent=2, allow_nan=False) + "\n")
    print(json.dumps(validation, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
