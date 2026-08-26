#!/usr/bin/env python3
"""Run one paper options tick. systemd supplies secrets; never prints them."""
import json

from kiwit.banknifty import BankNiftyService
from kiwit.brokers.groww import GrowwBrokerClient, GrowwSettings
from kiwit.database import DatabaseSettings, PostgresDatabase


def main():
    database = PostgresDatabase(DatabaseSettings.from_env())
    try:
        broker = GrowwBrokerClient(GrowwSettings.from_env())
    except ValueError:
        broker = None
    print(json.dumps(BankNiftyService(database, broker).run_once()))


if __name__ == '__main__':
    main()
