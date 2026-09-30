"""Import actual contract-note costs without treating estimates as broker invoices."""

import argparse
import csv
import json
import os
from decimal import Decimal
from pathlib import Path

from kiwit.database import DatabaseSettings, PostgresDatabase
from kiwit.options_evidence_gates import parse_contract_notes


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("csv_file", type=Path, help="Redacted CSV with position_id, trading_date, source_reference, actual_cost")
    args = parser.parse_args()
    notes = parse_contract_notes(list(csv.DictReader(args.csv_file.open())))
    db = PostgresDatabase(DatabaseSettings(os.environ["KIWIT_DATABASE_URL"]))
    imported = 0
    with db.transaction() as connection:
        for note in notes:
            result = connection.execute(
                "INSERT INTO banknifty_broker_cost_evidence(trading_date,position_id,source_reference,actual_cost,detail) "
                "VALUES(%s,%s,%s,%s,%s::jsonb) ON CONFLICT(position_id,source_reference) DO NOTHING RETURNING evidence_id",
                (note["trading_date"], note["position_id"], note["source_reference"], Decimal(note["actual_cost"]),
                 json.dumps({"import_file": args.csv_file.name, "components": note["components"],
                             "provenance": note["provenance"], "match_key": note["match_key"]})),
            ).fetchone()
            imported += bool(result)
    print(json.dumps({"rows": len(notes), "imported": imported, "ledger_rewritten": False}))


if __name__ == "__main__":
    main()
