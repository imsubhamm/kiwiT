BEGIN;

ALTER TABLE banknifty_worker_incidents
    ADD COLUMN first_seen_at timestamptz,
    ADD COLUMN last_seen_at timestamptz,
    ADD COLUMN occurrence_count integer NOT NULL DEFAULT 1 CHECK(occurrence_count > 0),
    ADD COLUMN recovered_at timestamptz,
    ADD COLUMN active boolean NOT NULL DEFAULT false,
    ADD COLUMN latest_detail jsonb NOT NULL DEFAULT '{}',
    ADD COLUMN recovery_reason_code text,
    ADD COLUMN recovery_detail jsonb,
    ADD COLUMN closed_at timestamptz,
    ADD COLUMN closure_reason text CHECK(closure_reason IN ('recovered','reason_changed'));

UPDATE banknifty_worker_incidents
SET first_seen_at = observed_at,
    last_seen_at = observed_at,
    latest_detail = detail,
    recovered_at = CASE WHEN status = 'recovered' THEN observed_at END,
    closed_at = CASE WHEN status = 'recovered' THEN observed_at END,
    closure_reason = CASE WHEN status = 'recovered' THEN 'recovered' END;

ALTER TABLE banknifty_worker_incidents
    ALTER COLUMN first_seen_at SET NOT NULL,
    ALTER COLUMN last_seen_at SET NOT NULL,
    ADD CONSTRAINT banknifty_worker_incidents_active_failure
        CHECK(NOT active OR status = 'failed'),
    ADD CONSTRAINT banknifty_worker_incidents_recovery_time
        CHECK(status <> 'recovered' OR recovered_at IS NOT NULL);

CREATE UNIQUE INDEX banknifty_worker_incidents_one_active_worker_idx
    ON banknifty_worker_incidents(worker) WHERE active;
CREATE INDEX banknifty_worker_incidents_lifecycle_idx
    ON banknifty_worker_incidents(worker, reason_code, last_seen_at DESC);

INSERT INTO schema_migrations(version,name) VALUES(18,'worker_incident_lifecycle');
COMMIT;
