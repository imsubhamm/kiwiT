"""Build a versioned supervised dataset from recorded market-v1 history."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from kiwit.marketdata.canonical import Instrument, Session, TradingCalendar
from kiwit.marketdata.history import HistoricalStore
from kiwit.ml.datasets import DatasetBuilder, LabelSpec, TimeSplits


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--calendar", type=Path, required=True)
    parser.add_argument("--labels", type=Path, help="Optional JSON LabelSpec")
    parser.add_argument("--output", type=Path, default=ROOT / "data/local/datasets")
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--exchange", default="NSE")
    parser.add_argument("--segment", default="CASH")
    parser.add_argument("--series", default="EQ")
    for name in ("start", "train-end", "validation-end", "test-end", "as-of"):
        parser.add_argument(f"--{name}", type=datetime.fromisoformat, required=True)
    args = parser.parse_args()
    config = json.loads(args.calendar.read_text())
    calendar = TradingCalendar(
        config["version"],
        tuple(
            Session(
                date.fromisoformat(s["day"]),
                time.fromisoformat(s.get("opens", "09:15")),
                time.fromisoformat(s.get("closes", "15:30")),
            )
            for s in config["sessions"]
        ),
    )
    labels = LabelSpec(**json.loads(args.labels.read_text())) if args.labels else LabelSpec()
    builder = DatasetBuilder(HistoricalStore(args.db), calendar, labels=labels)
    result = builder.build(
        Instrument(args.symbol, args.exchange, args.segment, args.series),
        args.start,
        TimeSplits(args.train_end, args.validation_end, args.test_end),
        as_of=args.as_of,
    )
    destination = builder.save(result, args.output)
    print(
        json.dumps(
            {"path": str(destination), "fingerprint": result["fingerprint"], "report": result["report"]}, indent=2
        )
    )


if __name__ == "__main__":
    main()
