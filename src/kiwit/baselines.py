from __future__ import annotations

from dataclasses import dataclass
from math import sqrt

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class BaselineResult:
    strategy_id: str
    metrics: dict[str, float | int | str]
    trades: pd.DataFrame
    equity_curve: pd.DataFrame


def _simulate(data: pd.DataFrame, entry_signal: pd.Series, exit_signal: pd.Series, strategy_id: str, initial_cash: float, cost_bps: float) -> BaselineResult:
    cash = initial_cash
    quantity = 0
    entry_price = 0.0
    entry_date = None
    pending_entry = pending_exit = False
    trades: list[dict] = []
    curve: list[dict] = []
    exposure_days = 0
    rate = cost_bps / 10_000

    for i, (day, row) in enumerate(data.iterrows()):
        if pending_exit and quantity:
            exit_price = float(row["open"])
            proceeds = quantity * exit_price * (1 - rate)
            pnl = proceeds - quantity * entry_price * (1 + rate)
            cash += proceeds
            trades.append({"entry_date": entry_date, "exit_date": day, "entry": entry_price, "exit": exit_price, "quantity": quantity, "pnl": pnl})
            quantity = 0
            pending_exit = False
        if pending_entry and quantity == 0:
            entry_price = float(row["open"])
            quantity = int(cash // (entry_price * (1 + rate)))
            if quantity:
                cash -= quantity * entry_price * (1 + rate)
                entry_date = day
            pending_entry = False
        if quantity:
            exposure_days += 1
            if bool(exit_signal.iloc[i]):
                pending_exit = True
        elif bool(entry_signal.iloc[i]):
            pending_entry = True
        curve.append({"date": day, "equity": cash + quantity * float(row["close"])})

    if quantity:
        day = data.index[-1]
        exit_price = float(data["close"].iloc[-1])
        proceeds = quantity * exit_price * (1 - rate)
        pnl = proceeds - quantity * entry_price * (1 + rate)
        cash += proceeds
        trades.append({"entry_date": entry_date, "exit_date": day, "entry": entry_price, "exit": exit_price, "quantity": quantity, "pnl": pnl})
        curve[-1]["equity"] = cash

    trade_frame = pd.DataFrame(trades)
    equity = pd.DataFrame(curve).set_index("date")
    daily = equity["equity"].pct_change().fillna(0)
    drawdown = equity["equity"] / equity["equity"].cummax() - 1
    years = max((equity.index[-1] - equity.index[0]).days / 365.25, 1 / 365.25)
    total_return = equity["equity"].iloc[-1] / initial_cash - 1
    gains = trade_frame.loc[trade_frame.pnl > 0, "pnl"].sum() if len(trade_frame) else 0
    losses = -trade_frame.loc[trade_frame.pnl < 0, "pnl"].sum() if len(trade_frame) else 0
    metrics = {
        "strategy_id": strategy_id,
        "start": str(equity.index[0].date()), "end": str(equity.index[-1].date()),
        "total_return_pct": float(total_return * 100),
        "cagr_pct": float(((1 + total_return) ** (1 / years) - 1) * 100),
        "max_drawdown_pct": float(drawdown.min() * 100),
        "sharpe_annualized": float(sqrt(252) * daily.mean() / daily.std()) if daily.std() else 0.0,
        "trade_count": int(len(trade_frame)),
        "win_rate_pct": float((trade_frame.pnl > 0).mean() * 100) if len(trade_frame) else 0.0,
        "profit_factor": float(gains / losses) if losses else float("inf"),
        "exposure_pct": float(exposure_days / len(data) * 100),
        "cost_bps_each_side": cost_bps,
    }
    return BaselineResult(strategy_id, metrics, trade_frame, equity)


def ema_baseline(
    data: pd.DataFrame,
    initial_cash: float = 1_000_000,
    cost_bps: float = 10,
    fast_span: int = 50,
    slow_span: int = 200,
) -> BaselineResult:
    if not 1 < fast_span < slow_span:
        raise ValueError("EMA spans must satisfy 1 < fast < slow")
    close = data["close"]
    ema50 = close.ewm(span=fast_span, adjust=False).mean()
    ema200 = close.ewm(span=slow_span, adjust=False).mean()
    regime = (close > ema200) & (ema50 > ema200)
    prior_regime = regime.shift(1).astype("boolean").fillna(False).astype(bool)
    warm = pd.Series(np.arange(len(data)) >= slow_span - 1, index=data.index)
    entry = regime & ~prior_regime & warm
    exit_ = (close < ema200) | (ema50 < ema200)
    return _simulate(data, entry, exit_, f"ema_{fast_span}_{slow_span}", initial_cash, cost_bps)


def donchian_baseline(
    data: pd.DataFrame,
    initial_cash: float = 1_000_000,
    cost_bps: float = 10,
    entry_window: int = 50,
    exit_window: int = 20,
) -> BaselineResult:
    if not 1 < exit_window < entry_window:
        raise ValueError("Donchian windows must satisfy 1 < exit < entry")
    prior_high = data["high"].rolling(entry_window).max().shift(1)
    prior_low = data["low"].rolling(exit_window).min().shift(1)
    entry = data["close"] > prior_high
    exit_ = data["close"] < prior_low
    return _simulate(data, entry, exit_, f"donchian_{entry_window}_{exit_window}", initial_cash, cost_bps)
