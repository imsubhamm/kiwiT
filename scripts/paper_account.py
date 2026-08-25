from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from kiwit.database import DatabaseSettings, PostgresDatabase
from kiwit.paper_trading import PostgresPaperLedger


def main() -> None:
    parser = argparse.ArgumentParser(description="Manage kiwiT paper accounts")
    parser.add_argument("command", choices=("create", "status", "halt", "resume"))
    parser.add_argument("--account", default="kiwit-paper-main")
    parser.add_argument("--cash", default="1000000")
    parser.add_argument("--reason", default="operator requested halt")
    parser.add_argument("--operator", default="local-operator")
    args = parser.parse_args()
    ledger = PostgresPaperLedger(PostgresDatabase(DatabaseSettings.from_env()))
    if args.command == "create":
        ledger.create_account(args.account, Decimal(args.cash))
    elif args.command == "halt":
        ledger.halt(args.account, "OPERATOR_HALT", args.reason)
    elif args.command == "resume":
        ledger.release_halt(args.account, args.operator)
    print(json.dumps(ledger.account_status(args.account), indent=2))


if __name__ == "__main__":
    main()
