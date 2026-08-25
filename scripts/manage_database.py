from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kiwit.database import DatabaseSettings, PostgresDatabase


def main() -> None:
    parser = argparse.ArgumentParser(description="Manage the kiwiT PostgreSQL database")
    parser.add_argument("command", choices=("migrate", "health"))
    parser.add_argument("--migrations", default="migrations")
    args = parser.parse_args()
    database = PostgresDatabase(DatabaseSettings.from_env())
    if args.command == "migrate":
        applied = database.migrate(args.migrations)
        print(json.dumps({"status": "ok", "applied_versions": applied}))
    else:
        print(json.dumps(database.healthcheck()))


if __name__ == "__main__":
    main()
