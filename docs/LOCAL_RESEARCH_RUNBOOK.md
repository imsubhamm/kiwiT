# kiwiT Local Research Runbook

## Safety check

```bash
PYTHONPATH=src python -m kiwit.cli doctor
```

Expected state is `environment=research` and `live_execution_enabled=false`.

## Tests

```bash
PYTHONPATH=src python -m unittest discover -s tests -v
```

## Official archive ingestion

```bash
python scripts/download_nse_data.py --start 2015-01-01 --end 2024-07-05
```

This uses legacy dated NSE archives. The July 2024 UDiFF transition requires a separate adapter before extending the period.

## Baselines

```bash
python scripts/run_baselines.py
```

The script:

1. Hashes and registers the raw NIFTYBEES archive dataset.
2. Registers the corporate-action adjustment hash in metadata.
3. Registers immutable strategy versions.
4. Runs next-session EMA and Donchian baselines with 10 bps cost per side.
5. Persists immutable run fingerprints in `data/local/kiwit_research.sqlite3`.
6. Writes human-readable results under `output/backtests/baselines_v1/`.

Repeating an identical run does not overwrite its metrics. A changed rule requires a new strategy version; changed code or data creates a new run fingerprint.

## PostgreSQL

`migrations/001_initial.sql` is intended for PostgreSQL staging. Do not apply it to a production database until migration tooling, credentials, backup/restore, and rollback procedures exist. SQLite is for local research only.

