# kiwiT Database Model

`migrations/001_initial.sql` is the production PostgreSQL schema. It stores source datasets by content hash, point-in-time instruments and index membership, corporate actions, immutable strategy specifications, reproducible backtest runs, trades, and audit events.

Local development uses `LocalResearchStore`, a narrow SQLite adapter with the same immutability concepts. SQLite is not the production database; it allows the research pipeline and tests to run before infrastructure is provisioned.

## Production setup

PostgreSQL 17 is the production database. Credentials are provided only through environment variables and must never be committed.

```bash
cp .env.example .env
# Replace the example password, then load the variables into your shell.
docker compose up -d postgres
pip install -e '.[production]'
python scripts/manage_database.py migrate
python scripts/manage_database.py health
```

`migrate` obtains a PostgreSQL advisory lock so only one application instance can migrate at a time. Applied migration files are checksummed; changing an already-applied migration fails closed. Create a new numbered migration for every schema change.

Production should use a managed PostgreSQL service with encryption at rest, TLS required in `KIWIT_DATABASE_URL`, automated point-in-time recovery, private networking, and separate owner/runtime/read-only users. The application runtime must not use the schema-owner account.

## Data boundaries

- Market truth: `datasets`, point-in-time `instruments`, `daily_bars`, `corporate_actions`, and `index_membership`.
- Reproducible research: immutable strategy versions, backtests, research runs, dataset links, and trades.
- Execution ledger: proposals, deterministic risk decisions, broker orders, and fills.
- Safety/operations: portfolio snapshots, system halts, ingestion runs, and hash-chained audit events.
- `audit_events` and `risk_decisions` are append-only at the database level.

Money and prices use PostgreSQL `numeric`; never binary floating point. Timestamps use `timestamptz` and are written in UTC. Exchange session dates remain `date` values.

## Backup and recovery acceptance criteria

- Daily automated backups plus point-in-time recovery.
- Quarterly restore drill into an isolated database.
- Recovery point objective: 15 minutes or better.
- Recovery time objective: 4 hours or better before paper/live execution is enabled.
- A database outage must stop new orders; it must never fall back to an untracked execution path.

## Invariants

- A dataset hash is registered once.
- A strategy version is immutable; a change requires a new version.
- A backtest fingerprint binds strategy version, dataset IDs, code hash, and parameters.
- Re-running the same fingerprint cannot overwrite prior metrics.
- Raw exchange files remain unchanged. Adjustments are derived and identified in metadata.
- Point-in-time membership uses validity intervals and cannot be replaced by today's constituent list.
