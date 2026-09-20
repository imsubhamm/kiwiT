BEGIN;
CREATE OR REPLACE VIEW banknifty_trade_outcomes AS
WITH exits AS (
 SELECT trading_date, detail, kind,
        COALESCE(detail->>'experiment_id','legacy-unbound') experiment_id,
        COALESCE(detail->>'playbook_id','legacy_unattributed') playbook_id,
        COALESCE(detail->>'settled_at',detail->>'exited_at',detail->'quote'->>'stamp',event_at::text)::timestamptz exited_at
 FROM banknifty_events WHERE kind IN ('paper_exit','paper_settlement') AND detail ? 'position_id'
)
SELECT detail->>'position_id' position_id, playbook_id, experiment_id,
       min(trading_date) session_day, min(exited_at) first_exit_at, max(exited_at) last_exit_at,
       sum((detail->>'pnl')::numeric) pnl,
       max((detail->>'capital')::numeric) capital,
       bool_or(kind='paper_settlement' OR COALESCE((detail->>'closed')::boolean,false)) closed,
       bool_or(kind='paper_settlement' OR (exited_at AT TIME ZONE 'Asia/Kolkata')::date <> trading_date) recovery
FROM exits GROUP BY detail->>'position_id',playbook_id,experiment_id;
INSERT INTO schema_migrations(version,name) VALUES(15,'banknifty_settlement');
COMMIT;
