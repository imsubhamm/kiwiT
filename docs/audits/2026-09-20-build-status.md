# Options recovery build — 20 September 2026

Branch: `codex/options-recovery-and-validation`. Working copy:
`/Users/imsub/kiwit-recovered` (the configured external-drive workspace was unusable).
These changes have been tested locally and prepared for review; they have not been merged or deployed.

## Implemented

- Authenticated expiry settlement bound to a position ID, with operator evidence,
  fees, duplicate protection, recovery attribution and later report supplements.
- Independent decision-worker health, fair report retries, recovery of abandoned
  AI calls from either decisions or reports, and visible retained-charge notices.
- Full plan-content replay plus entry validation and entry/partial-exit accounting.
  External consent/halt history and settlement-source authenticity are not replayed.
- Experiment identity excludes deployment SHA while retaining model, prompt,
  selector, sizing, exit policy and session risk/capital settings.
- Deployments pause timers, drain workers, stop the API before migrations and switch
  code only afterward. Rollback restores exact prior units and timer enablement,
  removes newly introduced units and stops new workers before switching back.
- Weekly direction becomes context; either 5m or 15m can support entry. Eight
  playbooks and five nearby strikes expand the candidate set. Sizing allows 50%
  premium allocation and 2% planned risk; whole lots, available cash/depth and session
  risk limits still apply. These caps are not guaranteed realized-loss limits.
- Two-minute AI slots, 180-second chart and 90-second quote freshness, and $5/day
  plus $50 per rolling 30 IST calendar days replace the lifetime AI allowance.
- CI installation uses the dependency lock; lint includes all source and tests.
- Frozen options-evidence exports include observations and reports. An offline
  evaluator compares AI/HOLD with the first eligible plan using future executable
  quotes, explicit exclusions and cost stress. This is a fixed-horizon opportunity
  study, not a portfolio backtest or broker-cost validation.
- Logical restore tool, deployment-session evidence checker, tape retention plan,
  calendar ownership runbook and 60-day calendar-expiry notice.

## Verification

All 464 Python/PostgreSQL regression tests and 23 browser tests passed, as did
Ruff, shell syntax and Git whitespace checks.

An actual local logical dump/restore completed against the migrated test database:
43 user tables matched source counts and row-content SHA-256 hashes, using a single
source snapshot. Duration: 0.71 seconds. Archive SHA-256:
`20cde26eb142e7c68a58e8f7574f6815f3cb0a8e006826938bc42cccc16cdc01`.
Private local evidence: `.audit-tools/restore-drill-20260920b/result.json`.
This is not a production restore drill or managed PITR verification.

## Outstanding

1. Production Neon refused the database connection with compute-time quota exceeded.
   Restore service availability before production verification or deployment.
2. Apply migration 015 and run the complete deployed market-session evidence chain.
   Tests use fake broker/AI/mail adapters; no real session completion is claimed.
3. Confirm the provider's PITR retention and perform an isolated production-data
   restore, recording the selected timestamp, duration and reconciliation results.
4. Supply real options bid/ask/depth evidence and reconciled broker costs; run
   chronological held-out AI-versus-baseline comparisons. No profitability is claimed.
5. ML/V2/RL integration into the options desk and empirical validation remain open.
   Their separate research tests do not establish options compatibility or promotion.
   No trained model artifacts or adequate evaluation dataset were supplied here.
6. Apply `docs/audits/2026-09-20-workflow-update.patch` using a credential with
   workflow scope. The current credential lacks that scope, so the PR carries the
   patch rather than changing Actions files. Local workflow edits are preserved.
7. Record a named calendar/storage owner and measured capacity/growth in the
   deployment log. Default tape policy retains all data; no history was deleted.

`neondb_owner` remains the accepted database role and is not treated as a blocker.
See [acceptance runbook](../OPTIONS_ACCEPTANCE_RUNBOOK.md) for execution steps.
