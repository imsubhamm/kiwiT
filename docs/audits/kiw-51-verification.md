# KIW-51 implementation and verification

Candidate quote retention is now derived from shared evaluator constants:
90 seconds for delayed entry + 60 minutes maximum horizon + 90 seconds exit
quote grace = 63 minutes. Explicit longer retention, including six hours for
opened positions, remains intact. Upserts preserve the maximum existing expiry.

The observer tracks ordinary candidates even without a running trading session.
Scans retain eligible and calendar-blocked plans. Model-selected plans, alternatives
not selected by the model, HOLD rejections, and execution-validation rejections
are classified separately. Class membership can overlap. Tracking-only contracts
are not renewed merely because their quote was observed after universe departure.

The 64 retained-symbol slots use deterministic priority: opened positions first,
then first-seen time, then symbol. Overflow obligations stay in the database;
there is no silent deletion or rotation. The current near-strike candidate universe
is still quoted independently, so 64 is the retained-symbol capacity, not a total
broker-request cap. An overflow symbol may still have a quote through that universe.

Each observer snapshot and heartbeat records required and selected symbols,
actual requested retained symbols, overflow count/list, observation time, and
per-class observed counts and missing symbols. Operations status and dashboard
expose this evidence. Missing includes capacity exclusions, absent instrument
catalogue entries, and unavailable/invalid quotes; no missing price is synthesized.
A successfully fetched quote does not imply an evaluable counterfactual: existing
spread, depth, timing, contract-integrity and market-close checks still apply.

Late-day retention deadlines are not truncated at close. Observations still stop
at market close, and evaluations lacking an eligible intraday exit retain their
explicit `future_exit_quote_or_depth_missing` exclusion.

Validation on 2026-09-29:

- Focused Python suites: 160 passed, 64 PostgreSQL-dependent tests skipped.
- Dashboard suite: 14 passed.
- Ruff on changed Python files and git diff --check passed.
- Tests cover every tracking class, deterministic overflow, opened-position
  priority, missing quotes, universe departure in the actual market adapter,
  the full delayed-entry/60-minute/exit-grace boundary, late-day exclusions,
  and decision-loop selected/HOLD/execution-rejected classification.

Commands:

```sh
PYTHONPATH=src python3 -m pytest -q tests/test_options_retention.py tests/test_options_evaluation.py tests/test_options_operations.py tests/test_entry_blocker_status.py tests/test_options_remediation.py tests/test_playbooks.py tests/test_banknifty.py tests/test_chart_analysis.py
node --test tests/banknifty-dashboard.test.cjs
python3 -m ruff check src/kiwit/options_retention.py src/kiwit/options_evaluation.py src/kiwit/banknifty.py src/kiwit/options_operations.py src/kiwit/playbooks.py tests/test_options_retention.py tests/test_options_operations.py
git diff --check
```

Release state: implemented on codex/kiw-51 from main b6e0956; not deployed or
runtime accepted. Before acceptance, run the PostgreSQL cases using
KIWIT_TEST_DATABASE_URL, deploy the reviewed commit, verify the actual release,
and inspect recorded coverage through a full horizon and a capacity overflow.
Paper-only execution and broker order boundaries remain unchanged.
