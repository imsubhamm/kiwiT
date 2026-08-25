from __future__ import annotations

from dataclasses import asdict, dataclass
from math import floor

import numpy as np
import pandas as pd


@dataclass
class Trade:
    signal_date: str
    entry_date: str
    exit_date: str
    entry: float
    exit: float
    quantity: int
    initial_risk: float
    pnl: float
    r_multiple: float
    exit_reason: str


def _rsi(series: pd.Series, length: int = 5) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / length, adjust=False).mean()
    loss = -delta.clip(upper=0).ewm(alpha=1 / length, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(100)


def _atr(frame: pd.DataFrame, length: int = 14) -> pd.Series:
    previous = frame["close"].shift(1)
    true_range = pd.concat(
        [(frame["high"] - frame["low"]), (frame["high"] - previous).abs(), (frame["low"] - previous).abs()],
        axis=1,
    ).max(axis=1)
    return true_range.ewm(alpha=1 / length, adjust=False).mean()


def prepare(equity: pd.DataFrame, index: pd.DataFrame) -> pd.DataFrame:
    e = equity.copy()
    n = index.copy()
    e["date"] = pd.to_datetime(e["date"])
    n["date"] = pd.to_datetime(n["date"])
    e = e.sort_values("date").set_index("date")
    n = n.sort_values("date").set_index("date").add_prefix("index_")
    data = e.join(n[["index_close"]], how="inner")
    data["index_ema50"] = data["index_close"].ewm(span=50, adjust=False).mean()
    data["index_ema200"] = data["index_close"].ewm(span=200, adjust=False).mean()
    data["ema50"] = data["close"].ewm(span=50, adjust=False).mean()
    data["ema200"] = data["close"].ewm(span=200, adjust=False).mean()
    data["atr14"] = _atr(data)
    data["rsi5"] = _rsi(data["close"], 5)
    data["rolling_high20"] = data["close"].rolling(20).max()
    data["days_since_peak"] = data["close"].rolling(21).apply(lambda x: len(x) - 1 - int(np.argmax(x)), raw=True)
    data["regime"] = (
        (data["index_close"] > data["index_ema200"])
        & (data["index_ema50"] > data["index_ema200"])
        & (data["index_ema200"] > data["index_ema200"].shift(20))
        & (data["close"] > data["ema200"])
        & (data["ema50"] > data["ema200"])
    )
    data["pullback"] = (
        data["days_since_peak"].between(2, 8)
        & (data["close"] >= data["ema50"] - 0.5 * data["atr14"])
        & (data["close"] < data["rolling_high20"])
        & (data["rsi5"] <= 45)
    )
    prior_pullback = data["pullback"].shift(1).astype("boolean").fillna(False).astype(bool)
    data["signal"] = data["regime"] & prior_pullback & (data["close"] > data["high"].shift(1))
    return data.dropna().copy()


def run_backtest(
    data: pd.DataFrame,
    initial_equity: float = 1_000_000,
    risk_fraction: float = 0.0025,
    cost_bps_each_side: float = 10,
) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    cash = initial_equity
    quantity = 0
    entry = stop = target = initial_risk = trade_cashflow = 0.0
    original_quantity = 0
    entry_date = signal_date = None
    entry_index = -1
    partial_taken = False
    trades: list[Trade] = []
    curve = []

    rows = list(data.iterrows())
    pending_signal = None
    for i, (day, row) in enumerate(rows):
        if pending_signal is not None and quantity == 0:
            setup_i, signal_day = pending_signal
            planned_entry = float(row["open"])
            setup_slice = data.iloc[max(0, setup_i - 8): setup_i + 1]
            planned_stop = float(setup_slice["low"].min() - 0.25 * data.iloc[setup_i]["atr14"])
            per_unit_risk = planned_entry - planned_stop
            if per_unit_risk > 0:
                risk_budget = cash * risk_fraction
                proposed = floor(risk_budget / (per_unit_risk + planned_entry * cost_bps_each_side / 10_000))
                affordable = floor(cash / (planned_entry * (1 + cost_bps_each_side / 10_000)))
                quantity = max(0, min(proposed, affordable))
                if quantity:
                    entry = planned_entry
                    stop = planned_stop
                    target = entry + per_unit_risk
                    initial_risk = per_unit_risk * quantity
                    original_quantity = quantity
                    entry_date = day
                    entry_index = i
                    signal_date = signal_day
                    entry_debit = entry * quantity * (1 + cost_bps_each_side / 10_000)
                    cash -= entry_debit
                    trade_cashflow = -entry_debit
                    partial_taken = False
            pending_signal = None

        if quantity:
            exit_price = None
            reason = None
            if row["open"] <= stop:
                exit_price, reason = float(row["open"]), "gap_stop"
            elif row["low"] <= stop:
                exit_price, reason = stop, "stop"
            elif not partial_taken and row["high"] >= target:
                sold = max(1, quantity // 2)
                partial_credit = target * sold * (1 - cost_bps_each_side / 10_000)
                cash += partial_credit
                trade_cashflow += partial_credit
                quantity -= sold
                partial_taken = True
                stop = max(stop, entry)
            elif i - entry_index >= 15 and row["close"] < entry + 0.5 * (target - entry):
                exit_price, reason = float(row["close"]), "time_stop"
            elif partial_taken and i >= 10:
                trailing = float(data.iloc[i - 10:i]["low"].min())
                stop = max(stop, trailing)

            if exit_price is not None:
                exit_value = exit_price * quantity * (1 - cost_bps_each_side / 10_000)
                cash += exit_value
                trade_cashflow += exit_value
                pnl = trade_cashflow
                trades.append(Trade(
                    signal_date=str(signal_date.date()), entry_date=str(entry_date.date()), exit_date=str(day.date()),
                    entry=entry, exit=exit_price, quantity=original_quantity, initial_risk=initial_risk,
                    pnl=pnl, r_multiple=pnl / initial_risk if initial_risk else 0, exit_reason=reason,
                ))
                quantity = 0

        if quantity == 0 and bool(row["signal"]):
            pending_signal = (i, day)

        mark = cash + quantity * float(row["close"])
        curve.append({"date": day, "equity": mark})

    trade_frame = pd.DataFrame([asdict(t) for t in trades])
    curve_frame = pd.DataFrame(curve).set_index("date")
    returns = curve_frame["equity"].pct_change().fillna(0)
    years = max((curve_frame.index[-1] - curve_frame.index[0]).days / 365.25, 1 / 365.25)
    drawdown = curve_frame["equity"] / curve_frame["equity"].cummax() - 1
    summary = {
        "start": str(curve_frame.index[0].date()),
        "end": str(curve_frame.index[-1].date()),
        "initial_equity": initial_equity,
        "final_equity": float(curve_frame["equity"].iloc[-1]),
        "total_return_pct": float((curve_frame["equity"].iloc[-1] / initial_equity - 1) * 100),
        "cagr_pct": float(((curve_frame["equity"].iloc[-1] / initial_equity) ** (1 / years) - 1) * 100),
        "max_drawdown_pct": float(drawdown.min() * 100),
        "trade_count": int(len(trade_frame)),
        "win_rate_pct": float((trade_frame["pnl"] > 0).mean() * 100) if len(trade_frame) else 0.0,
        "sharpe_annualized": float(np.sqrt(252) * returns.mean() / returns.std()) if returns.std() else 0.0,
        "cost_bps_each_side": cost_bps_each_side,
    }
    return trade_frame, curve_frame, summary
