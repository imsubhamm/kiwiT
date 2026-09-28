"""Flat-only rollout barrier, compatible with releases predating this module.

Table locks serialize with *existing* writers; no application rollout is needed
before the barrier works. Only use on the single systemd application host.
"""
from __future__ import annotations

import subprocess
import time


class DeploymentDeferred(RuntimeError):
    pass


def systemctl(*args):
    return subprocess.run(
        ["systemctl", *args], capture_output=True, text=True, timeout=30, check=False
    )


def quiesce(database, units, *, control=systemctl, clock=time.monotonic, sleep=time.sleep, timeout=120):
    """Return with all producers stopped and no exposure, or restore scheduling.

    SHARE locks exclude INSERT/UPDATE/DELETE, including transactions already in
    flight. A worker blocked on a lock cannot drain: we abort and release the
    locks before restoring timers, never kill it to make a deployment proceed.
    """
    timers = []
    api_active = False
    changed = False

    def command(*args):
        result = control(*args)
        if result.returncode:
            raise RuntimeError(f"systemctl {' '.join(args)} failed")
        return result.stdout.strip()

    try:
        with database.transaction() as connection:
            connection.execute("SET LOCAL lock_timeout = '5s'")
            connection.execute("SET LOCAL statement_timeout = '10s'")
            connection.execute(
                "LOCK TABLE banknifty_sessions, paper_positions, intraday_signals IN SHARE MODE"
            )
            row = connection.execute(
                "SELECT EXISTS (SELECT 1 FROM banknifty_sessions "
                "WHERE state->'position' IS NOT NULL AND state->'position' <> 'null'::jsonb), "
                "EXISTS (SELECT 1 FROM paper_positions WHERE quantity <> 0), "
                "EXISTS (SELECT 1 FROM intraday_signals WHERE status = 'entered')"
            ).fetchone()
            if row is None or any(row):
                raise DeploymentDeferred("open or inconsistent paper exposure; deployment deferred")
            for unit in units:
                result = control("is-active", f"{unit}.timer")
                if result.stdout.strip() == "active":
                    timers.append(unit)
            api_active = control("is-active", "kiwit-api").stdout.strip() == "active"
            changed = True
            for unit in units:
                command("stop", f"{unit}.timer")
            command("stop", "kiwit-api")
            deadline = clock() + timeout
            while True:
                busy = False
                for unit in units:
                    state = command("show", f"{unit}.service", "-p", "ActiveState", "--value")
                    if state not in {"inactive", "failed"}:
                        busy = True
                if not busy:
                    break
                if clock() >= deadline:
                    raise DeploymentDeferred("workers did not drain; deployment deferred")
                sleep(1)
            # An in-flight watchdog may have restarted the API while draining.
            command("stop", "kiwit-api")
            if command("show", "kiwit-api", "-p", "ActiveState", "--value") != "inactive":
                raise DeploymentDeferred("API did not stop; deployment deferred")
            # Check the connection still owns the transaction before declaring success.
            connection.execute("SELECT 1")
    except BaseException:
        # The transaction/locks have been released before workers resume.
        if changed:
            failures = []
            for unit in sorted(timers, key=lambda unit: "supervisor" not in unit):
                if control("start", f"{unit}.timer").returncode:
                    failures.append(unit)
            if api_active and control("start", "kiwit-api").returncode:
                failures.append("kiwit-api")
            if failures:
                raise RuntimeError(f"deployment aborted; could not restore: {', '.join(failures)}")
        raise
