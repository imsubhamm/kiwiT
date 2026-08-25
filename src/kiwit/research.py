from __future__ import annotations

from dataclasses import dataclass
from itertools import product
from typing import Callable

import numpy as np
import pandas as pd

from .baselines import BaselineResult, _simulate, donchian_baseline, ema_baseline


StrategyFn = Callable[[pd.DataFrame, float, float], BaselineResult]


@dataclass(frozen=True)
class PromotionGate:
    name: str
    passed: bool
    observed: float | int | str
    requirement: str


def passive_benchmark(data: pd.DataFrame, initial_cash: float = 1_000_000, cost_bps: float = 10) -> BaselineResult:
    entry = pd.Series(False, index=data.index)
    entry.iloc[0] = True
    exit_ = pd.Series(False, index=data.index)
    return _simulate(data, entry, exit_, "passive_buy_hold", initial_cash, cost_bps)


def strategy_signals(data: pd.DataFrame, strategy: str, parameters: dict[str, int]) -> tuple[pd.Series, pd.Series]:
    if strategy == "ema":
        fast, slow = parameters["fast_span"], parameters["slow_span"]
        close = data["close"]
        fast_ema = close.ewm(span=fast, adjust=False).mean()
        slow_ema = close.ewm(span=slow, adjust=False).mean()
        regime = (close > slow_ema) & (fast_ema > slow_ema)
        prior = regime.shift(1).astype("boolean").fillna(False).astype(bool)
        warm = pd.Series(np.arange(len(data)) >= slow - 1, index=data.index)
        return regime & ~prior & warm, (close < slow_ema) | (fast_ema < slow_ema)
    if strategy == "donchian":
        high = data["high"].rolling(parameters["entry_window"]).max().shift(1)
        low = data["low"].rolling(parameters["exit_window"]).min().shift(1)
        return data["close"] > high, data["close"] < low
    raise ValueError(f"unknown strategy: {strategy}")


def randomized_entry_control(
    data: pd.DataFrame,
    strategy: str,
    parameters: dict[str, int],
    simulations: int = 1000,
    seed: int = 20260825,
    initial_cash: float = 1_000_000,
    cost_bps: float = 10,
) -> dict:
    actual_entry, exit_signal = strategy_signals(data, strategy, parameters)
    target_entries = max(1, int(actual_entry.sum()))
    warmup = max(parameters.values())
    candidate_positions = np.arange(warmup, max(warmup + 1, len(data) - 1))
    rng = np.random.default_rng(seed)
    returns = []
    sharpes = []
    for _ in range(simulations):
        selected = rng.choice(candidate_positions, size=min(target_entries, len(candidate_positions)), replace=False)
        entry = pd.Series(False, index=data.index)
        entry.iloc[selected] = True
        result = _simulate(data, entry, exit_signal, "random_control", initial_cash, cost_bps)
        returns.append(result.metrics["total_return_pct"])
        sharpes.append(result.metrics["sharpe_annualized"])
    actual = _simulate(data, actual_entry, exit_signal, f"{strategy}_actual", initial_cash, cost_bps)
    values = np.asarray(returns)
    actual_return = float(actual.metrics["total_return_pct"])
    return {
        "simulations": simulations,
        "seed": seed,
        "actual_return_pct": actual_return,
        "random_return_median_pct": float(np.median(values)),
        "random_return_p05_pct": float(np.percentile(values, 5)),
        "random_return_p95_pct": float(np.percentile(values, 95)),
        "actual_percentile_vs_random": float((values <= actual_return).mean() * 100),
        "random_positive_fraction": float((values > 0).mean()),
        "actual_sharpe": float(actual.metrics["sharpe_annualized"]),
        "random_sharpe_median": float(np.median(sharpes)),
    }


def fixed_walk_forward(data: pd.DataFrame, runner: StrategyFn, cost_bps: float = 10) -> list[dict]:
    folds = [
        ("2016-2018", "2016-01-01", "2018-12-31"),
        ("2019-2021", "2019-01-01", "2021-12-31"),
        ("2022-2024", "2022-01-01", "2024-12-31"),
        ("2025-2026", "2025-01-01", "2026-08-20"),
    ]
    output = []
    for name, start, end in folds:
        evaluation = data.loc[start:end]
        if len(evaluation) < 50:
            continue
        # Preserve all history before the fold so slow indicators are warmed using
        # information genuinely available at the fold boundary.
        result = runner(data.loc[:end], 1_000_000, cost_bps)
        curve = result.equity_curve.loc[start:end, "equity"]
        if curve.empty:
            continue
        normalized = curve / curve.iloc[0]
        daily = normalized.pct_change().fillna(0)
        drawdown = normalized / normalized.cummax() - 1
        years = max((curve.index[-1] - curve.index[0]).days / 365.25, 1 / 365.25)
        total_return = normalized.iloc[-1] - 1
        trades = result.trades.copy()
        if len(trades):
            exit_dates = pd.to_datetime(trades["exit_date"])
            trades = trades[(exit_dates >= pd.Timestamp(start)) & (exit_dates <= pd.Timestamp(end))]
        gains = trades.loc[trades.pnl > 0, "pnl"].sum() if len(trades) else 0
        losses = -trades.loc[trades.pnl < 0, "pnl"].sum() if len(trades) else 0
        output.append({
            "fold": name, "start": str(curve.index[0].date()), "end": str(curve.index[-1].date()),
            "total_return_pct": float(total_return * 100),
            "cagr_pct": float(((1 + total_return) ** (1 / years) - 1) * 100),
            "max_drawdown_pct": float(drawdown.min() * 100),
            "sharpe_annualized": float(np.sqrt(252) * daily.mean() / daily.std()) if daily.std() else 0.0,
            "trade_count": int(len(trades)),
            "win_rate_pct": float((trades.pnl > 0).mean() * 100) if len(trades) else 0.0,
            "profit_factor": float(gains / losses) if losses else float("inf"),
            "cost_bps_each_side": cost_bps,
        })
    return output


def cost_stress(data: pd.DataFrame, runner: StrategyFn, costs: tuple[int, ...] = (0, 5, 10, 20, 30)) -> list[dict]:
    return [{"cost_bps_each_side": cost, **runner(data, 1_000_000, cost).metrics} for cost in costs]


def parameter_neighborhood(data: pd.DataFrame, strategy: str, cost_bps: float = 10) -> list[dict]:
    results = []
    if strategy == "ema":
        combinations = ({"fast_span": fast, "slow_span": slow} for fast, slow in product((40, 50, 60), (180, 200, 220)))
        for params in combinations:
            result = ema_baseline(data, cost_bps=cost_bps, **params)
            results.append({**params, **result.metrics})
    elif strategy == "donchian":
        combinations = ({"entry_window": entry, "exit_window": exit_} for entry, exit_ in product((40, 50, 60), (15, 20, 25)))
        for params in combinations:
            result = donchian_baseline(data, cost_bps=cost_bps, **params)
            results.append({**params, **result.metrics})
    else:
        raise ValueError(strategy)
    return results


def bootstrap_trades(trades: pd.DataFrame, simulations: int = 10000, seed: int = 20260825) -> dict:
    if trades.empty:
        return {"simulations": simulations, "trade_count": 0}
    denominators = trades["entry"] * trades["quantity"]
    trade_returns = (trades["pnl"] / denominators).to_numpy(dtype=float)
    rng = np.random.default_rng(seed)
    terminal = []
    maximum_drawdowns = []
    for _ in range(simulations):
        sample = rng.choice(trade_returns, size=len(trade_returns), replace=True)
        curve = np.cumprod(1 + sample)
        running_peak = np.maximum.accumulate(np.insert(curve, 0, 1.0))[1:]
        terminal.append(curve[-1] - 1)
        maximum_drawdowns.append(np.min(curve / running_peak - 1))
    return {
        "simulations": simulations, "seed": seed, "trade_count": len(trade_returns),
        "terminal_return_p05_pct": float(np.percentile(terminal, 5) * 100),
        "terminal_return_median_pct": float(np.median(terminal) * 100),
        "terminal_return_p95_pct": float(np.percentile(terminal, 95) * 100),
        "max_drawdown_p05_pct": float(np.percentile(maximum_drawdowns, 5) * 100),
        "probability_of_loss": float((np.asarray(terminal) < 0).mean()),
    }


def promotion_gates(result: BaselineResult, folds: list[dict], random_control: dict, neighborhood: list[dict]) -> list[PromotionGate]:
    positive_folds = sum(float(fold["total_return_pct"]) > 0 for fold in folds)
    positive_neighbors = sum(float(item["total_return_pct"]) > 0 for item in neighborhood)
    return [
        PromotionGate("minimum_trades", int(result.metrics["trade_count"]) >= 200, int(result.metrics["trade_count"]), ">= 200"),
        PromotionGate("positive_net_expectation", float(result.metrics["profit_factor"]) > 1.2, float(result.metrics["profit_factor"]), "> 1.20 after costs"),
        PromotionGate("walk_forward_majority", positive_folds >= max(1, len(folds) // 2 + 1), positive_folds, f">= {len(folds) // 2 + 1} positive folds"),
        PromotionGate("random_control_95th", float(random_control["actual_percentile_vs_random"]) >= 95, float(random_control["actual_percentile_vs_random"]), ">= 95th percentile"),
        PromotionGate("parameter_stability", positive_neighbors >= 7, positive_neighbors, ">= 7 of 9 positive neighbors"),
        PromotionGate("drawdown_limit", float(result.metrics["max_drawdown_pct"]) >= -15, float(result.metrics["max_drawdown_pct"]), ">= -15%"),
    ]
