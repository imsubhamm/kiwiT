BEGIN;
CREATE TABLE banknifty_tracked_contracts (
    trading_date date NOT NULL,
    symbol text NOT NULL,
    contract jsonb NOT NULL,
    first_seen_at timestamptz NOT NULL,
    last_selected_at timestamptz NOT NULL,
    retain_until timestamptz NOT NULL,
    reasons jsonb NOT NULL DEFAULT '[]',
    PRIMARY KEY (trading_date, symbol)
);
CREATE INDEX banknifty_tracked_contracts_active_idx
    ON banknifty_tracked_contracts(trading_date, retain_until DESC);

CREATE TABLE banknifty_worker_incidents (
    incident_id bigserial PRIMARY KEY,
    trading_date date NOT NULL,
    worker text NOT NULL,
    observed_at timestamptz NOT NULL,
    status text NOT NULL CHECK(status IN ('failed','recovered')),
    reason_code text NOT NULL,
    detail jsonb NOT NULL DEFAULT '{}'
);
CREATE INDEX banknifty_worker_incidents_day_idx
    ON banknifty_worker_incidents(trading_date, worker, observed_at DESC);

CREATE TABLE banknifty_broker_cost_evidence (
    evidence_id bigserial PRIMARY KEY,
    trading_date date NOT NULL,
    position_id text NOT NULL,
    source_reference text NOT NULL,
    actual_cost numeric NOT NULL CHECK(actual_cost >= 0),
    imported_at timestamptz NOT NULL DEFAULT now(),
    detail jsonb NOT NULL DEFAULT '{}',
    UNIQUE(position_id, source_reference)
);

INSERT INTO schema_migrations(version,name) VALUES(16,'banknifty_evidence_coverage');
COMMIT;
