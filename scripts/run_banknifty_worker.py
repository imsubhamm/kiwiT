#!/usr/bin/env python3
"""Run one paper options tick. systemd supplies secrets; never prints them."""
import argparse
import json

from kiwit.banknifty import BankNiftyService
from kiwit.brokers.groww import GrowwBrokerClient, GrowwSettings
from kiwit.database import DatabaseSettings, PostgresDatabase


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("decision", "observe", "supervise", "reports"), default="decision")
    mode = parser.parse_args().mode
    database = PostgresDatabase(DatabaseSettings.from_env())
    try:
        broker = GrowwBrokerClient(GrowwSettings.from_env())
    except ValueError:
        broker = None
    service = BankNiftyService(database, broker)
    if mode == "observe":
        result = service.observe()
    elif mode == "supervise":
        state = service.supervise()
        result = {"state": state["state"] if state else "idle"}
    elif mode == "reports":
        service._process_daily_report(None, service.clock())
        result = {"state": "reports_processed"}
    else:
        result = service.run_once()
    print(json.dumps(result))


if __name__ == '__main__':
    main()
