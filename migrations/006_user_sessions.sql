BEGIN;

CREATE TABLE IF NOT EXISTS app_users (
    user_id             text PRIMARY KEY,
    email               text NOT NULL UNIQUE,
    password_hash       text NOT NULL,
    role                text NOT NULL CHECK (role IN ('super_admin', 'operator', 'viewer')),
    active              boolean NOT NULL DEFAULT true,
    created_at          timestamptz NOT NULL DEFAULT now(),
    CHECK (email = lower(email))
);

CREATE TABLE IF NOT EXISTS app_sessions (
    session_id          text PRIMARY KEY,
    user_id             text NOT NULL REFERENCES app_users(user_id),
    token_sha256        char(64) NOT NULL UNIQUE,
    created_at          timestamptz NOT NULL,
    expires_at          timestamptz NOT NULL,
    revoked_at          timestamptz,
    remote_address      text NOT NULL DEFAULT '',
    CHECK (expires_at > created_at)
);

CREATE INDEX IF NOT EXISTS app_sessions_active_idx ON app_sessions(token_sha256, expires_at)
WHERE revoked_at IS NULL;

INSERT INTO schema_migrations(version, name) VALUES (6, 'user_sessions')
ON CONFLICT (version) DO UPDATE SET name = EXCLUDED.name;
COMMIT;
