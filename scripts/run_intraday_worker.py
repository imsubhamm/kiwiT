#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime

from kiwit.brokers.groww import GrowwBrokerClient, GrowwSettings
from kiwit.database import DatabaseSettings, PostgresDatabase
from kiwit.intraday import IntradayService
from kiwit.monitoring_schedule import active_monitoring_window


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--scheduled', action='store_true', help='Apply automatic database wake-up schedule')
    args = parser.parse_args()
    if args.scheduled and not active_monitoring_window(datetime.now(UTC)):
        print(json.dumps({'state': 'database_check_deferred'}))
        return
    database = PostgresDatabase(DatabaseSettings.from_env())
    broker = GrowwBrokerClient(GrowwSettings.from_env())
    result = IntradayService(database, broker).run_once()
    print(json.dumps(result, sort_keys=True))
    if result["state"] == "failed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
