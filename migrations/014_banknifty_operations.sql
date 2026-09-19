BEGIN;
ALTER TABLE banknifty_daily_reports ADD COLUMN delivery_lease uuid;
ALTER TABLE banknifty_daily_reports ADD COLUMN delivery_lease_until timestamptz;
ALTER TABLE banknifty_daily_reports ADD COLUMN delivery_next_attempt timestamptz;
CREATE TABLE banknifty_operational_alerts (
    reason_key text PRIMARY KEY,
    last_attempt timestamptz NOT NULL,
    delivery_status text NOT NULL
);
CREATE TABLE banknifty_worker_health (
    worker text PRIMARY KEY,
    observed_at timestamptz NOT NULL,
    status text NOT NULL,
    detail jsonb NOT NULL DEFAULT '{}'
);
-- Append-only economic ledger stays untouched. This view derives actual exit dates
-- from the executable quotes, including legacy overnight recovery trades.
CREATE VIEW banknifty_trade_outcomes AS
WITH exits AS (
 SELECT trading_date, detail,
        COALESCE(detail->>'experiment_id','legacy-unbound') experiment_id,
        COALESCE(detail->>'playbook_id','legacy_unattributed') playbook_id,
        COALESCE(detail->>'exited_at',detail->'quote'->>'stamp',event_at::text)::timestamptz exited_at
 FROM banknifty_events WHERE kind='paper_exit' AND detail ? 'position_id'
)
SELECT detail->>'position_id' position_id, playbook_id, experiment_id,
       min(trading_date) session_day, min(exited_at) first_exit_at, max(exited_at) last_exit_at,
       sum((detail->>'pnl')::numeric) pnl,
       max((detail->>'capital')::numeric) capital,
       bool_or(COALESCE((detail->>'closed')::boolean,false)) closed,
       bool_or((exited_at AT TIME ZONE 'Asia/Kolkata')::date <> trading_date) recovery
FROM exits GROUP BY detail->>'position_id',playbook_id,experiment_id;
CREATE INDEX banknifty_events_day_kind_idx ON banknifty_events(trading_date,kind);
INSERT INTO schema_migrations(version,name) VALUES(14,'banknifty_operations');
COMMIT;
