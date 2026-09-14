"""Version-filtered evaluation reports; never an automatic promotion decision."""

import argparse
import html
import json
from collections import defaultdict
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID

from .marketdata.canonical import IST, utc
from .ml.datasets import _hash


def number(value):
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError("Metrics require finite numbers")
    return result


def trade_metrics(rows):
    values = [number(r["net_pnl"]) for r in rows if r.get("net_pnl") is not None]
    gains = sum((v for v in values if v > 0), Decimal(0))
    losses = -sum((v for v in values if v < 0), Decimal(0))
    return {
        "closed_trades": len(values),
        "net_pnl": str(sum(values, Decimal(0))),
        "win_rate": sum(v > 0 for v in values) / len(values) if values else None,
        "expectancy": str(sum(values) / len(values)) if values else None,
        "profit_factor": str(gains / losses) if losses else None,
        "profit_factor_note": "No observed losses" if not losses else None,
    }


def from_backtest(report):
    """Import a checksum-verified KIW-30 report without inventing missing LLM evidence."""
    body = dict(report)
    if body.pop("fingerprint") != _hash(body) or body["version"] != "unified-replay-v1":
        raise ValueError("Invalid backtest report")
    rows = []
    orders = report["portfolio"]["orders"]
    for decision in report["decisions"]:
        candidate = decision["meta"].get("selected_candidate") or {}
        identifier = candidate.get("candidate_id")
        order = orders.get(str(UUID(identifier[:32])), {}) if identifier else {}
        rows.append(
            {
                "at": decision["at"],
                "status": decision["status"],
                "strategy": candidate.get("strategy", "UNKNOWN"),
                "regime": candidate.get("regime", "UNKNOWN"),
                "confidence": candidate.get("confidence"),
                "net_pnl": order.get("realized_pnl") if order.get("status") == "CLOSED" else None,
            }
        )
    return {
        "run_id": report["fingerprint"],
        "mode": "backtest",
        "models": sorted(v for v in (report["model_bindings"] or {}).values() if v),
        "prompt_version": "UNRECORDED",
        "config_version": _hash(report["configuration"]),
        "initial_equity": report["portfolio"]["metadata"]["initial_capital"],
        "equity_curve": report["equity_curve"],
        "rows": rows,
    }


def summarize(run):
    if run["mode"] not in {"paper", "backtest"}:
        raise ValueError("Explicit paper/backtest mode required")
    for key in ("run_id", "prompt_version", "config_version"):
        if not isinstance(run[key], str) or not run[key]:
            raise ValueError("Version bindings required")
    if not isinstance(run["models"], list) or any(not isinstance(v, str) or not v for v in run["models"]):
        raise ValueError("Model fingerprints must be a list of strings")
    rows = run["rows"]
    for row in rows:
        if not isinstance(row["status"], str) or not row["status"]:
            raise ValueError("Decision status required")
        if row.get("llm_verdict") not in {None, "APPROVE", "REJECT", "UNCERTAIN"}:
            raise ValueError("Unknown LLM verdict")
        if row.get("net_pnl") is not None and row["status"] != "SUBMITTED":
            raise ValueError("Realized P&L requires a submitted order")
    times = [utc(datetime.fromisoformat(row["at"])) for row in rows]
    if times != sorted(set(times)):
        raise ValueError("Decision timestamps must strictly increase")
    peak = number(run["initial_equity"])
    if peak <= 0:
        raise ValueError("Positive initial equity required")
    drawdown = Decimal(0)
    last = None
    for point in run["equity_curve"]:
        at = utc(datetime.fromisoformat(point["at"]))
        if last is not None and at <= last:
            raise ValueError("Equity timestamps must strictly increase")
        last = at
        equity = number(point["equity"])
        peak = max(peak, equity)
        drawdown = max(drawdown, (peak - equity) / peak)
    groups = {}
    for field in ("strategy", "regime", "time_of_day"):
        buckets = defaultdict(list)
        for row, at in zip(rows, times, strict=True):
            key = at.astimezone(IST).strftime("%H:00 IST") if field == "time_of_day" else row.get(field, "UNKNOWN")
            buckets[key].append(row)
        groups[field] = {key: trade_metrics(items) for key, items in sorted(buckets.items())}
    calibrated, bins = [], defaultdict(list)
    for row in rows:
        confidence = row.get("confidence")
        if confidence is not None:
            confidence = float(number(confidence))
            if not 0 <= confidence <= 1:
                raise ValueError("Confidence outside [0, 1]")
            if row.get("net_pnl") is not None:
                outcome = int(number(row["net_pnl"]) > 0)
                calibrated.append((confidence - outcome) ** 2)
                bins[min(int(confidence * 10), 9)].append((confidence, outcome))
    effects = {}
    for verdict in ("APPROVE", "REJECT", "UNCERTAIN"):
        selected = [r for r in rows if r.get("llm_verdict") == verdict]
        labelled = [r for r in selected if r.get("candidate_outcome") is not None]
        effects[verdict] = {
            "decisions": len(selected),
            "labelled": len(labelled),
            "candidate_wins": sum(number(r["candidate_outcome"]) > 0 for r in labelled),
            "candidate_losses": sum(number(r["candidate_outcome"]) < 0 for r in labelled),
        }
    latency = [number(r["latency_ms"]) for r in rows if r.get("latency_ms") is not None]
    costs = [number(r["llm_cost_usd"]) for r in rows if r.get("llm_cost_usd") is not None]
    if any(v < 0 for v in latency + costs):
        raise ValueError("Negative cost or latency")
    rejected = [r for r in rows if r["status"] != "SUBMITTED"]
    labels = [number(r["candidate_outcome"]) for r in rejected if r.get("candidate_outcome") is not None]
    midpoint = len(rows) // 2
    earlier, recent = trade_metrics(rows[:midpoint]), trade_metrics(rows[midpoint:])
    comparable = earlier["expectancy"] is not None and recent["expectancy"] is not None
    return {
        "run_id": run["run_id"],
        "mode": run["mode"],
        "models": run["models"],
        "prompt_version": run["prompt_version"],
        "config_version": run["config_version"],
        "metrics": {
            **trade_metrics(rows),
            "maximum_drawdown": str(drawdown),
            "submitted": sum(r["status"] == "SUBMITTED" for r in rows),
            "no_order_decisions": len(rejected),
            "no_trade": sum(r["status"] == "NO_TRADE" for r in rows),
        },
        "breakdowns": groups,
        "calibration": {
            "population": "Closed selected trades; selection-biased, net-positive outcome proxy",
            "labelled": len(calibrated),
            "brier_score": sum(calibrated) / len(calibrated) if calibrated else None,
            "bins": {
                str(k): {
                    "count": len(v),
                    "mean_confidence": sum(p for p, _ in v) / len(v),
                    "win_rate": sum(y for _, y in v) / len(v),
                }
                for k, v in bins.items()
            },
        },
        "rejection_outcomes": {
            "labelled": len(labels),
            "avoided_losses": sum(v < 0 for v in labels),
            "missed_wins": sum(v > 0 for v in labels),
        },
        "llm_effectiveness": effects,
        "telemetry": {
            "known_cost_usd": str(sum(costs, Decimal(0))),
            "cost_coverage": len(costs),
            "total_decisions": len(rows),
            "latency_coverage": len(latency),
            "mean_latency_ms": str(sum(latency) / len(latency)) if latency else None,
        },
        "degradation": {
            "earlier": earlier,
            "recent": recent,
            "expectancy_declined": number(recent["expectancy"]) < number(earlier["expectancy"]) if comparable else None,
            "statistical_significance": "NOT_ESTABLISHED",
        },
        "validation": "NOT_VALIDATED: profitability and sample count alone cannot authorize promotion",
    }


def evaluate(runs, *, mode=None, model=None, prompt=None, config=None):
    ids = [r["run_id"] for r in runs]
    if len(set(ids)) != len(ids):
        raise ValueError("Duplicate run IDs")
    selected = [
        r
        for r in runs
        if (mode is None or r["mode"] == mode)
        and (model is None or model in r["models"])
        and (prompt is None or r["prompt_version"] == prompt)
        and (config is None or r["config_version"] == config)
    ]
    return {
        "version": "evaluation-dashboard-v1",
        "filters": {"mode": mode, "model": model, "prompt": prompt, "config": config},
        "runs": [summarize(r) for r in selected],
    }


def render(report):
    sections = []
    for run in report["runs"]:
        sections.append(
            "<section><h2>"
            + html.escape(run["mode"].upper() + " · " + run["run_id"])
            + "</h2>"
            + "<p class='warning'>"
            + html.escape(run["validation"])
            + "</p>"
            + "".join(
                "<h3>"
                + html.escape(key.replace("_", " "))
                + "</h3><pre>"
                + html.escape(json.dumps(value, indent=2))
                + "</pre>"
                for key, value in run.items()
                if key not in {"run_id", "mode", "validation"}
            )
            + "</section>"
        )
    return (
        "<!doctype html><html lang='en'><meta charset='utf-8'><title>NitiQuant evaluation</title>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'><style>"
        "body{font:16px system-ui;background:#101824;color:#e6edf5;max-width:1100px;margin:40px auto;padding:20px}"
        "section{background:#1c2939;padding:24px;margin:24px 0;border-radius:12px}"
        "pre{white-space:pre-wrap;overflow-wrap:anywhere}.warning{color:#ffd080}h3{text-transform:capitalize}"
        "</style><h1>NitiQuant model &amp; strategy evaluation</h1><p>Runs remain separate. "
        "Missing outcomes and telemetry are unavailable, not zero evidence. Degradation is descriptive.</p>"
        "<p>Filters: "
        + html.escape(json.dumps(report["filters"]))
        + "</p>"
        + ("".join(sections) or "<p>No matching runs.</p>")
        + "</html>"
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", nargs="+", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--mode", choices=["paper", "backtest"])
    for key in ("model", "prompt", "config"):
        parser.add_argument("--" + key)
    args = parser.parse_args()
    runs = []
    for path in args.inputs:
        data = json.loads(path.read_text())
        runs.append(from_backtest(data) if data.get("version") == "unified-replay-v1" else data)
    report = evaluate(runs, mode=args.mode, model=args.model, prompt=args.prompt, config=args.config)
    args.output.write_text(render(report))
    args.output.with_suffix(".json").write_text(json.dumps(report, indent=2, allow_nan=False) + "\n")


if __name__ == "__main__":
    main()
