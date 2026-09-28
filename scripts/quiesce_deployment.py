#!/usr/bin/env python3
"""Root-only deployment barrier; database errors fail closed, without DSN output."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kiwit.database import DatabaseSettings, PostgresDatabase
from kiwit.deployment_gate import DeploymentDeferred, quiesce

if __name__ == "__main__":
    try:
        quiesce(PostgresDatabase(DatabaseSettings.from_env()), sys.argv[1:])
    except DeploymentDeferred as error:
        print(str(error), file=sys.stderr)
        sys.exit(75)
    except Exception:  # noqa: BLE001 -- never print database credentials in deployment logs
        print("deployment barrier failed; inspect database/systemd health before retrying", file=sys.stderr)
        sys.exit(1)
