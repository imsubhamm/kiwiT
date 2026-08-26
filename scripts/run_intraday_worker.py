#!/usr/bin/env python3
from __future__ import annotations

import json

from kiwit.brokers.groww import GrowwBrokerClient, GrowwSettings
from kiwit.database import DatabaseSettings, PostgresDatabase
from kiwit.intraday import IntradayService


def main() -> None:
    database = PostgresDatabase(DatabaseSettings.from_env())
    broker = GrowwBrokerClient(GrowwSettings.from_env())
    result = IntradayService(database, broker).run_once()
    print(json.dumps(result, sort_keys=True))
    if result["state"] == "failed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
