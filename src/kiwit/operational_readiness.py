"""Read-only, authenticated diagnostics; does not refresh tokens or authorize orders."""

import os
from datetime import UTC, datetime


def inspect_readiness(database, intraday, *, now=None):
    now = now or datetime.now(UTC)
    result = {
        "at": now.isoformat(), "execution": "paper-only", "new_execution_allowed": False,
        "release": os.getenv("KIWIT_RELEASE_SHA", "development"),
        "checks": {}, "reason_codes": [],
        "session_lifecycle": "configured" if os.getenv("KIWIT_SESSION_CONFIG") else "not_configured",
        "v2_model_readiness": "NOT_ASSESSED",
    }
    if database is None:
        result["checks"]["database"] = "UNAVAILABLE"
        result["reason_codes"].append("DATABASE_UNAVAILABLE")
    else:
        try:
            health = database.healthcheck()
            result["schema_version"] = health["schema_version"]
            result["checks"]["database"] = "OK"
            with database.connect(autocommit=True) as connection:
                rows = connection.execute(
                    "SELECT scope,reason_code FROM system_halts WHERE active ORDER BY scope,reason_code"
                ).fetchall()
            result["active_halts"] = [{"scope": scope, "reason_code": code} for scope, code in rows]
            result["checks"]["kill_switch"] = "HALTED" if rows else "CLEAR"
            if rows:
                result["reason_codes"].append("ACTIVE_EXECUTION_HALT")
        except Exception:  # noqa: BLE001 - diagnostics must not leak database credentials
            result["checks"]["database"] = "UNAVAILABLE"
            result["reason_codes"].append("DATABASE_OR_HALT_CHECK_FAILED")
    if intraday is None:
        result["checks"]["cash_paper_worker"] = "UNAVAILABLE"
        result["reason_codes"].append("CASH_PAPER_WORKER_UNAVAILABLE")
    else:
        try:
            data = intraday.freshness(now)
            fresh = bool(data["instruments"]) and all(i["fresh"] for i in data["instruments"])
            result["checks"]["cash_market_data"] = "FRESH" if fresh else "STALE_OR_MISSING"
            result["checks"]["cash_paper_worker"] = (data.get("worker") or {}).get("state", "NOT_OBSERVED")
            if not fresh:
                result["reason_codes"].append("CASH_MARKET_DATA_STALE_OR_MISSING")
            worker = data.get("worker") or {}
            if worker.get("state") in {"failed", "data_unavailable"}:
                result["reason_codes"].append("CASH_WORKER_FAILED_OR_DEGRADED")
        except Exception:  # noqa: BLE001 - expose reason codes, never raw upstream exceptions
            result["checks"]["cash_market_data"] = "UNKNOWN"
            result["reason_codes"].append("CASH_READINESS_CHECK_FAILED")
    result["status"] = "degraded" if result["reason_codes"] else "dependencies_observed"
    result["authorization"] = "Diagnostics only; session, strategy, model and risk gates still required"
    return result
