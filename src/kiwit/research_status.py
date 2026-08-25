from __future__ import annotations

from copy import deepcopy
from typing import Any

REGIME_ROUTER_V1: dict[str, Any] = {
    "strategy_id": "regime_router",
    "version": "1.0.0",
    "decision": "rejected",
    "promoted": False,
    "dataset": {
        "rows": 3_021_372,
        "symbols": 3_359,
        "start": "2020-01-01",
        "end": "2026-08-20",
        "point_in_time": True,
    },
    "regimes": [
        {"name": "bull_trend", "strategy": "20-day cross-sectional breakout"},
        {"name": "range", "strategy": "RSI(2) short-horizon pullback"},
        {"name": "risk_off", "strategy": "cash"},
    ],
    "metrics": {
        "trades": 2_670,
        "total_return_pct": 66.69,
        "profit_factor": 1.316,
        "median_trade_pct": 0.647,
        "win_rate_pct": 57.15,
        "max_drawdown_pct": -32.78,
        "random_entry_percentile": 74.5,
        "positive_folds": 2,
        "folds": 4,
        "positive_neighbors": 9,
        "neighbors": 9,
        "return_at_30bps_pct": 2.71,
    },
    "gates": [
        {"name": "Point-in-time universe", "passed": True, "actual": "3.02M dated observations"},
        {"name": "At least 200 clean trades", "passed": True, "actual": "2,670"},
        {"name": "Profit factor above 1.20", "passed": True, "actual": "1.316"},
        {"name": "Positive median trade after costs", "passed": True, "actual": "+0.647%"},
        {"name": "At least 3 of 4 positive folds", "passed": False, "actual": "2 / 4"},
        {"name": "Maximum drawdown no worse than −15%", "passed": False, "actual": "−32.78%"},
        {"name": "Positive at 30 bps per side", "passed": True, "actual": "+2.71%"},
        {"name": "At least 7 of 9 positive neighbours", "passed": True, "actual": "9 / 9"},
        {"name": "At least 95th percentile vs random", "passed": False, "actual": "74.5th"},
    ],
    "automation": {
        "state": "locked",
        "reason": "Strategy failed 3 mandatory evidence gates",
        "schedule": "Weekdays after NSE close; exchange-session validation required",
        "manual_start_required": False,
        "groww_execution": "disabled",
    },
}


def regime_router_status() -> dict[str, Any]:
    return deepcopy(REGIME_ROUTER_V1)
