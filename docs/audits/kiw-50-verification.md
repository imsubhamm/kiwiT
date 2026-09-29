# KIW-50 verification

Implementation: local branch `codex/kiw-50`, based on origin/main at
`a845620`. Not merged, deployed, or runtime accepted.

Calendar classification now comes from `options_events.entry_calendar_blocker`
for plan selection, entry gates, and execution checks. Strategy readiness uses
that module's configuration-blocker set, including invalid calendars. Entry scan
records retain the specific calendar validation cause; readiness and session
status share the actionable message shown by the dashboard. Readiness reports
the first observation and duration of the current cause, resetting on recovery
or a change in cause. Historical scans without detailed evidence remain readable.
Liveness and paper-only execution boundaries are unchanged.

Validation on 2026-09-29:

- Focused Python suite: 103 passed, 64 skipped (PostgreSQL not configured).
- Dashboard Node suite: 13 passed.
- Ruff checks for changed Python files and `git diff --check`: passed.
- Coverage includes missing, malformed, stale and uncovered calendars, direct
  invalid input, actionable session messages, independent healthy liveness,
  recovery, changed causes, historical scans, and ordinary NO_ELIGIBLE_PLAN.

Commands:

```sh
PYTHONPATH=src python3 -m pytest -q tests/test_options_operations.py tests/test_entry_blocker_status.py tests/test_playbooks.py tests/test_options_remediation.py tests/test_banknifty.py
node --test tests/banknifty-dashboard.test.cjs
python3 -m ruff check src/kiwit/options_events.py src/kiwit/options_policy.py src/kiwit/options_operations.py src/kiwit/playbooks.py tests/test_options_operations.py tests/test_entry_blocker_status.py
git diff --check
```

Before runtime acceptance, run the PostgreSQL integration cases with
`KIWIT_TEST_DATABASE_URL`, deploy the reviewed commit, verify the running release,
and observe invalid-calendar degradation and subsequent recovery in status and
the dashboard. No deployed-release verification was performed here.

Workspace: `/Users/imsub/Projects/kiwit-kiw50`. The original Flash checkout was
not edited during this fix: its HEAD and new branch reference read as NUL bytes.
