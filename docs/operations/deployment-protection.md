# Flat-only deployment and rollback (KIW-47)

Activation and migration are deferred while any Bank Nifty session contains a
position, any paper account has nonzero quantity, or an intraday signal is still
entered. The checks cover every account and trading date, including inconsistent
exposure records. Deployment does not request an exit or change trading state.

The deployment helper takes PostgreSQL SHARE locks on all three position stores
before reading them. Existing writers must finish before the check; later writers
cannot commit until the barrier releases. This protects the first deployment from
older application code too: writers need no new advisory-lock convention.

With flat books, timers are stopped and workers drain while the locks remain held.
The API is stopped, and stopped again after drain to account for an in-flight
watchdog. Only after all producers stop are the locks released and migrations run.
A writer waiting behind the barrier prevents drain and causes a deferred deployment;
it is never killed to let deployment proceed. Database/lock/systemd failures fail
closed, with scheduling restored after the transaction releases its locks. Missing
position tables also fail closed; this upgrade path is not a bootstrap installer.

Rollback uses the same barrier. If a new release has already opened a position,
rollback is deferred and the current release is retained with its existing
supervision. The operator must investigate readiness and retry after natural
closure. Never force-close a position to deploy or roll back. Supervision starts
before the API is exposed on activation and on restoration of the previous release.

This protocol requires the existing single-host systemd topology: the API and the
listed workers are all writers. Do not manually launch workers or restart services
while deployment holds its host lock. A multi-host deployment needs a shared entry
fence before using this procedure. Migrations must preserve compatibility with the
previous release; automatic code rollback does not reverse database migrations.

`/opt/kiwit/deployment-history.log` records the requested full SHA, previous and
current release paths, current full SHA, outcome and a conservative upper bound on
the supervision scheduling gap in seconds. The bound starts before gate acquisition
and ends after scheduling is restored (or rollback is deferred); it includes lock
wait, drain, migration and readiness. It is not a measured quote-to-exit latency.
For open-position deferrals no timer is stopped. Treat restore failures as requiring
operator intervention; the helper reports them and deployment exits unsuccessfully.

Tests cover all three exposure stores, PostgreSQL writers racing before and after
the flat check, lock failures, drain timeout, partial systemd failures, rollback
ordering, and rollback deferral. PostgreSQL tests use only
`KIWIT_TEST_DATABASE_URL` and disposable schemas. No live broker execution is added.
