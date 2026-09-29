# KIW-48: scan opportunity evaluation

Rule and exit matrices consume ordinary `strategy_scan.plans`, calendar-blocked
`shadow_plans`, and legacy AI-call plans. The occurrence key hashes trading date,
selection timestamp, and frozen plan ID. Duplicate scan/call copies count once;
scan evidence takes precedence and later selections remain distinct occurrences.
Without a selection timestamp, scan availability (or a call plan's creation time)
is the legacy identity fallback.

Scan outcomes start at event availability; call-only outcomes start at response
settlement. Existing quote freshness, plan integrity, depth, and cost checks apply.
Each exported observation retains source ID, availability and selection times,
entry gate and block reasons, provenance, and the observed or excluded outcome.
All exported opportunities are evidence-only and non-executable. Execution gates
and signed plans are unchanged.

New scan events retain capital and release provenance. Old scan capital can be
recovered from an exact date/selection-time/plan-ID match in a retained market or
AI snapshot. Current session balances are never substituted. Missing or invalid
capital, missing availability, and unavailable forward quotes are explicit
exclusions, rather than disappearing from denominators.

Rule scenarios report attempted, included, excluded, and per-occurrence exclusion
reasons, including scenario cap and loss limits. Exit scenarios report those
counts and exclusion-reason counts for each playbook/horizon. These overlapping
fixed-horizon studies are not portfolio returns or promotion evidence. The paired
AI comparison is unchanged; common-stream AI comparison is separate KIW-56 work.

Validation covers cap, cooldown, no-call scans, calendar shadows, scan/call
deduplication, repeated selections, missing context/quotes, legacy snapshot
recovery, and scenario exclusions. The database-backed entry-cap test also checks
that scans retain capital while their entry gate stays blocked.

Release state: implementation only; not merged, deployed, or runtime accepted.
