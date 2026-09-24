"""Import actual contract-note costs without treating estimates as broker invoices."""

import argparse
import csv
import json
import os
from decimal import Decimal
from pathlib import Path

from kiwit.database import DatabaseSettings, PostgresDatabase


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("csv_file", type=Path, help="CSV: trading_date,position_id,actual_cost,source_reference")
    args = parser.parse_args()
    rows = list(csv.DictReader(args.csv_file.open()))
    db = PostgresDatabase(DatabaseSettings(os.environ["KIWIT_DATABASE_URL"]))
    imported = 0
    with db.transaction() as connection:
        for row in rows:
            cost = Decimal(row["actual_cost"])
            if not cost.is_finite() or cost < 0 or not row["source_reference"].strip():
                raise ValueError("Every row needs a non-negative actual_cost and source_reference")
            result = connection.execute(
                "INSERT INTO banknifty_broker_cost_evidence(trading_date,position_id,source_reference,actual_cost,detail) "
                "VALUES(%s,%s,%s,%s,%s::jsonb) ON CONFLICT(position_id,source_reference) DO NOTHING RETURNING evidence_id",
                (row["trading_date"], row["position_id"], row["source_reference"].strip(), cost,
                 json.dumps({"import_file": args.csv_file.name})),
            ).fetchone()
            imported += bool(result)
    print(json.dumps({"rows": len(rows), "imported": imported}))


if __name__ == "__main__":
    main()
