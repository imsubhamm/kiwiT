BEGIN;

ALTER TABLE schema_migrations ADD COLUMN IF NOT EXISTS name text;
ALTER TABLE schema_migrations ADD COLUMN IF NOT EXISTS checksum char(64);

CREATE TABLE IF NOT EXISTS ingestion_runs (
    ingestion_run_id    uuid PRIMARY KEY,
    source              text NOT NULL,
    status              text NOT NULL CHECK (status IN ('started', 'succeeded', 'failed')),
    requested_start     date,
    requested_end       date,
    dataset_id          uuid REFERENCES datasets(dataset_id),
    error_message       text,
    started_at          timestamptz NOT NULL,
    completed_at        timestamptz,
    metadata            jsonb NOT NULL DEFAULT '{}'::jsonb,
    CHECK (completed_at IS NULL OR completed_at >= started_at),
    CHECK ((status = 'started' AND completed_at IS NULL) OR status <> 'started')
);

CREATE TABLE IF NOT EXISTS research_runs (
    research_run_id     uuid PRIMARY KEY,
    run_fingerprint     char(64) NOT NULL UNIQUE,
    strategy_id         text NOT NULL,
    strategy_version    text NOT NULL,
    dataset_id          uuid NOT NULL REFERENCES datasets(dataset_id),
    code_sha256         char(64) NOT NULL,
    configuration       jsonb NOT NULL,
    results             jsonb NOT NULL,
    promotion_decision  text NOT NULL CHECK (promotion_decision IN ('promoted', 'rejected', 'insufficient_evidence')),
    started_at          timestamptz NOT NULL,
    completed_at        timestamptz NOT NULL,
    created_at          timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (strategy_id, strategy_version) REFERENCES strategy_versions(strategy_id, version),
    CHECK (completed_at >= started_at)
);

CREATE TABLE IF NOT EXISTS trade_proposals (
    proposal_id         uuid PRIMARY KEY,
    idempotency_key     text NOT NULL UNIQUE,
    strategy_id         text NOT NULL,
    strategy_version    text NOT NULL,
    instrument_id       uuid NOT NULL REFERENCES instruments(instrument_id),
    side                text NOT NULL CHECK (side IN ('buy', 'sell')),
    signal_at           timestamptz NOT NULL,
    entry_price         numeric(24, 8) NOT NULL CHECK (entry_price > 0),
    stop_price          numeric(24, 8) NOT NULL CHECK (stop_price > 0),
    target_price        numeric(24, 8) CHECK (target_price > 0),
    status              text NOT NULL CHECK (status IN ('received', 'risk_rejected', 'awaiting_human', 'approved', 'expired', 'submitted')),
    rationale           jsonb NOT NULL DEFAULT '{}'::jsonb,
    expires_at          timestamptz,
    created_at          timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (strategy_id, strategy_version) REFERENCES strategy_versions(strategy_id, version),
    CHECK ((side = 'buy' AND stop_price < entry_price) OR (side = 'sell' AND stop_price > entry_price))
);

CREATE TABLE IF NOT EXISTS risk_decisions (
    risk_decision_id    uuid PRIMARY KEY,
    proposal_id         uuid NOT NULL REFERENCES trade_proposals(proposal_id),
    decision            text NOT NULL CHECK (decision IN ('approve', 'reject', 'no_trade')),
    quantity            numeric(30, 8) NOT NULL CHECK (quantity >= 0),
    risk_budget         numeric(24, 8) NOT NULL CHECK (risk_budget >= 0),
    estimated_loss      numeric(24, 8) NOT NULL CHECK (estimated_loss >= 0),
    reason_codes        jsonb NOT NULL,
    rules_version       text NOT NULL,
    decided_at          timestamptz NOT NULL,
    UNIQUE (proposal_id, rules_version)
);

CREATE TABLE IF NOT EXISTS broker_orders (
    order_id            uuid PRIMARY KEY,
    proposal_id         uuid NOT NULL REFERENCES trade_proposals(proposal_id),
    client_order_id     text NOT NULL UNIQUE,
    broker              text NOT NULL,
    broker_order_id     text,
    environment         text NOT NULL CHECK (environment IN ('paper', 'live')),
    order_type          text NOT NULL CHECK (order_type IN ('market', 'limit', 'stop', 'stop_limit')),
    side                text NOT NULL CHECK (side IN ('buy', 'sell')),
    quantity            numeric(30, 8) NOT NULL CHECK (quantity > 0),
    limit_price         numeric(24, 8),
    stop_price          numeric(24, 8),
    status              text NOT NULL CHECK (status IN ('created', 'submitted', 'accepted', 'partially_filled', 'filled', 'cancelled', 'rejected', 'expired')),
    submitted_at        timestamptz,
    updated_at          timestamptz NOT NULL,
    raw_response        jsonb NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (broker, broker_order_id)
);

CREATE TABLE IF NOT EXISTS fills (
    fill_id             uuid PRIMARY KEY,
    order_id            uuid NOT NULL REFERENCES broker_orders(order_id),
    broker_fill_id      text,
    filled_at           timestamptz NOT NULL,
    quantity            numeric(30, 8) NOT NULL CHECK (quantity > 0),
    price               numeric(24, 8) NOT NULL CHECK (price > 0),
    fees                numeric(24, 8) NOT NULL DEFAULT 0 CHECK (fees >= 0),
    taxes               numeric(24, 8) NOT NULL DEFAULT 0 CHECK (taxes >= 0),
    raw_response        jsonb NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (order_id, broker_fill_id)
);

CREATE TABLE IF NOT EXISTS portfolio_snapshots (
    snapshot_id         uuid PRIMARY KEY,
    account_id          text NOT NULL,
    captured_at         timestamptz NOT NULL,
    equity              numeric(24, 8) NOT NULL CHECK (equity >= 0),
    cash                numeric(24, 8) NOT NULL,
    realized_daily_pnl  numeric(24, 8) NOT NULL,
    realized_weekly_pnl numeric(24, 8) NOT NULL,
    high_watermark      numeric(24, 8) NOT NULL CHECK (high_watermark >= 0),
    open_risk           numeric(24, 8) NOT NULL CHECK (open_risk >= 0),
    positions           jsonb NOT NULL DEFAULT '[]'::jsonb,
    UNIQUE (account_id, captured_at)
);

CREATE TABLE IF NOT EXISTS system_halts (
    halt_id             uuid PRIMARY KEY,
    scope               text NOT NULL,
    reason_code         text NOT NULL,
    reason              text NOT NULL,
    active              boolean NOT NULL DEFAULT true,
    activated_at        timestamptz NOT NULL,
    released_at         timestamptz,
    released_by         text,
    CHECK ((active AND released_at IS NULL) OR (NOT active AND released_at IS NOT NULL))
);

CREATE INDEX IF NOT EXISTS ingestion_runs_status_idx ON ingestion_runs (status, started_at);
CREATE INDEX IF NOT EXISTS research_runs_strategy_idx ON research_runs (strategy_id, strategy_version, completed_at);
CREATE INDEX IF NOT EXISTS proposals_status_idx ON trade_proposals (status, created_at);
CREATE INDEX IF NOT EXISTS orders_status_idx ON broker_orders (status, updated_at);
CREATE INDEX IF NOT EXISTS fills_order_idx ON fills (order_id, filled_at);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_system_halt_idx ON system_halts (scope) WHERE active;

CREATE OR REPLACE FUNCTION prevent_mutation() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION '% is append-only', TG_TABLE_NAME;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS audit_events_append_only ON audit_events;
CREATE TRIGGER audit_events_append_only BEFORE UPDATE OR DELETE ON audit_events
FOR EACH ROW EXECUTE FUNCTION prevent_mutation();

DROP TRIGGER IF EXISTS risk_decisions_append_only ON risk_decisions;
CREATE TRIGGER risk_decisions_append_only BEFORE UPDATE OR DELETE ON risk_decisions
FOR EACH ROW EXECUTE FUNCTION prevent_mutation();

INSERT INTO schema_migrations(version, name) VALUES (2, 'operational')
ON CONFLICT (version) DO UPDATE SET name = EXCLUDED.name;
COMMIT;
