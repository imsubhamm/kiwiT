BEGIN;

CREATE TABLE IF NOT EXISTS strategy_promotions (
    promotion_id        uuid PRIMARY KEY,
    strategy_id         text NOT NULL,
    strategy_version    text NOT NULL,
    run_fingerprint     char(64) NOT NULL,
    report_sha256       char(64) NOT NULL,
    evidence_gates      jsonb NOT NULL,
    approved_by         text NOT NULL CHECK (length(trim(approved_by)) > 0),
    approval_reason     text NOT NULL CHECK (length(trim(approval_reason)) > 0),
    approved_at         timestamptz NOT NULL,
    created_at          timestamptz NOT NULL DEFAULT now(),
    UNIQUE (strategy_id, strategy_version),
    FOREIGN KEY (strategy_id, strategy_version) REFERENCES strategy_versions(strategy_id, version),
    CHECK (jsonb_typeof(evidence_gates) = 'object')
);

DROP TRIGGER IF EXISTS strategy_promotions_append_only ON strategy_promotions;
CREATE TRIGGER strategy_promotions_append_only BEFORE UPDATE OR DELETE ON strategy_promotions
FOR EACH ROW EXECUTE FUNCTION prevent_mutation();

INSERT INTO schema_migrations(version, name) VALUES (5, 'strategy_promotions')
ON CONFLICT (version) DO UPDATE SET name = EXCLUDED.name;
COMMIT;
