"""Read models and durable operational state for the paper options desk."""
import json
from datetime import datetime, timedelta
from uuid import uuid4

from .intraday import IST
from .options_calendar import regular_session


def heartbeat(store, worker, now, status, detail=None):
    with store.locked() as connection:
        connection.execute(
            "INSERT INTO banknifty_worker_health(worker,observed_at,status,detail) VALUES(%s,%s,%s,%s::jsonb) "
            "ON CONFLICT(worker) DO UPDATE SET observed_at=EXCLUDED.observed_at,status=EXCLUDED.status,detail=EXCLUDED.detail",
            (worker, now, status, json.dumps(detail or {}, default=str)))


def report_backlog(service, now):
    """Catch up all due dates; SMTP is at-least-once with a bounded claim lease."""
    local = now.astimezone(IST)
    today_due = (local.hour, local.minute) >= (15, 30)
    with service.store.locked() as connection:
        sessions = connection.execute(
            "SELECT s.state FROM banknifty_sessions s LEFT JOIN banknifty_daily_reports r USING(trading_date) "
            "WHERE r.trading_date IS NULL AND (s.trading_date<%s OR (s.trading_date=%s AND %s)) "
            "ORDER BY s.trading_date LIMIT 20", (local.date(), local.date(), today_due)).fetchall()
        for (state,) in sessions:
            service.store.daily_report(connection, state, now)
    # Do not spend attempts while SMTP has no configuration. Tests use a simple adapter.
    if not getattr(service.mailer, "configured", True):
        return
    lease = uuid4()
    with service.store.locked() as connection:
        row = connection.execute(
            "SELECT trading_date,report FROM banknifty_daily_reports WHERE delivery_status<>'sent' "
            "AND (delivery_lease_until IS NULL OR delivery_lease_until<=%s) "
            "AND (delivery_next_attempt IS NULL OR delivery_next_attempt<=%s) "
            "ORDER BY trading_date LIMIT 1 FOR UPDATE SKIP LOCKED", (now, now)).fetchone()
        if not row:
            return
        day, report = row
        connection.execute(
            "UPDATE banknifty_daily_reports SET delivery_lease=%s,delivery_lease_until=%s WHERE trading_date=%s",
            (lease, now + timedelta(minutes=5), day))
    try:
        status, error = service.mailer.send_daily_report(report, service.dashboard_url)
    except Exception:  # noqa: BLE001 - release lease with safe evidence for arbitrary mail adapters
        status, error = "failed", "Email adapter failure"
    with service.store.locked() as connection:
        connection.execute(
            "UPDATE banknifty_daily_reports SET delivery_status=%s,delivery_attempts=delivery_attempts+%s,"
            "delivery_attempted_at=%s,delivery_error=%s,delivery_lease=NULL,delivery_lease_until=NULL,"
            "delivery_next_attempt=%s WHERE trading_date=%s AND delivery_lease=%s",
            (status, 0 if status == "not_configured" else 1, now, error[:500],
             None if status == "sent" else now + timedelta(minutes=15), day, lease))
    return report


def diagnostics(store, now):
    day = now.astimezone(IST).date()
    local = now.astimezone(IST)
    in_session = regular_session(day) is True and (9, 30) <= (local.hour, local.minute) < (15, 30)
    with store.locked() as connection:
        workers = connection.execute("SELECT worker,observed_at,status,detail FROM banknifty_worker_health").fetchall()
        state = store.latest(connection)
        calls = connection.execute(
            "SELECT state,result FROM banknifty_ai_calls WHERE trading_date=%s ORDER BY created_at DESC LIMIT 3",
            (day,)).fetchall()
        pending = connection.execute(
            "SELECT count(*) FROM banknifty_daily_reports WHERE delivery_status<>'sent'").fetchone()[0]
        outcome = connection.execute(
            "SELECT count(*),COALESCE(sum(pnl),0) FROM banknifty_trade_outcomes WHERE recovery").fetchone()
        reasons = connection.execute(
            "SELECT evaluation->>'playbook_id',reason,count(*) FROM banknifty_events "
            "CROSS JOIN LATERAL jsonb_array_elements(detail->'evaluations') evaluation "
            "CROSS JOIN LATERAL jsonb_array_elements_text(evaluation->'reasons') reason "
            "WHERE kind='strategy_scan' AND trading_date=%s AND NOT (evaluation->>'eligible')::boolean "
            "GROUP BY 1,2", (day,)).fetchall()
        counts = connection.execute(
            "SELECT kind,count(*) FROM banknifty_events WHERE trading_date=%s GROUP BY kind", (day,)).fetchall()
        owned = connection.execute(
            "SELECT EXISTS(SELECT 1 FROM pg_tables WHERE schemaname=current_schema() AND tableowner=current_user)"
        ).fetchone()[0]
    health = {w: {"at": at.isoformat(), "status": status, "age_seconds": (now-at).total_seconds(), "detail": detail}
              for w, at, status, detail in workers}
    issues = []
    if in_session:
        for worker in ("supervisor", "observer"):
            if worker not in health or health[worker]["age_seconds"] > 150:
                issues.append(worker.upper() + "_HEARTBEAT_STALE")
            elif health[worker]["status"] == "failed":
                issues.append(worker.upper() + "_FAILED")
        observer_stamp = health.get("observer", {}).get("detail", {}).get("spot_at")
        if observer_stamp and (now - datetime.fromisoformat(observer_stamp)).total_seconds() > 150:
            issues.append("OBSERVED_MARKET_DATA_STALE")
    if len(calls) == 3 and all(status == "failed" for status, _ in calls):
        issues.append("AI_CONSECUTIVE_FAILURES")
    if state and state.get("position"):
        if state["day"] != str(day):
            issues.append("OVERDUE_POSITION")
        if in_session and not state.get("valuation_fresh"):
            issues.append("POSITION_QUOTE_STALE")
    if pending:
        issues.append("REPORT_DELIVERY_PENDING")
    if owned:
        issues.append("RUNTIME_OWNS_TABLES")
    if regular_session(day) is None:
        issues.append("CALENDAR_UNVERIFIED")
    return {"status": "degraded" if issues else "ok", "reason_codes": issues, "workers": health,
            "in_session": in_session, "pending_reports": pending,
            "recovery_trades": outcome[0], "recovery_pnl": str(outcome[1]),
            "funnel": dict(counts), "rejection_counts": [
                {"playbook_id": p, "reason": r, "count": n} for p,r,n in reasons]}
