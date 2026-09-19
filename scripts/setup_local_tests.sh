#!/usr/bin/env bash
set -euo pipefail
# Point this at an isolated database. Never substitute the production URL.
: "${KIWIT_TEST_DATABASE_URL:?Set an isolated PostgreSQL test database URL}"
export TMPDIR=${KIWIT_TEST_TMPDIR:-/tmp}
python3 -m venv .venv
.venv/bin/python -m pip install -c requirements.lock -e '.[api,production,workflow,research,ml,llm,dev]'
.venv/bin/python -m pytest -q
node --test tests/*.test.cjs
