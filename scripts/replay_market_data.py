"""Import canonical JSONL or emit point-in-time historical replay frames."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from kiwit.marketdata.canonical import Instrument, Session, TradingCalendar
from kiwit.marketdata.history import HistoricalStore, replay_sessions


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=ROOT / "data/local/history.sqlite3")
    sub = parser.add_subparsers(dest="command", required=True)
    imp = sub.add_parser("import", help="Import market-v1 JSONL with known availability")
    imp.add_argument("--file", type=Path, required=True)
    imp.add_argument("--available-at", type=datetime.fromisoformat, required=True)
    imp.add_argument("--source", required=True)
    replay = sub.add_parser("replay", help="Emit canonical frames at each declared candle close")
    replay.add_argument("--calendar", type=Path, required=True)
    replay.add_argument("--start", type=datetime.fromisoformat, required=True)
    replay.add_argument("--end", type=datetime.fromisoformat, required=True)
    replay.add_argument("--symbol", required=True)
    replay.add_argument("--exchange", default="NSE")
    replay.add_argument("--segment", default="CASH")
    replay.add_argument("--series", default="EQ")
    replay.add_argument("--interval-minutes", type=int, default=1)
    args = parser.parse_args()
    store = HistoricalStore(args.db)
    if args.command == "import":
        count = store.import_jsonl(args.file, available_at=args.available_at, source=args.source)
        print(json.dumps({"inserted": count}))
        return
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
    instrument = Instrument(args.symbol, args.exchange, args.segment, args.series)
    for frame in replay_sessions(
        store, calendar, instrument, args.start, args.end, interval=timedelta(minutes=args.interval_minutes)
    ):
        print(
            json.dumps(
                {
                    "as_of": frame.as_of.isoformat(),
                    "calendar_version": frame.calendar_version,
                    "invalid_market_state": frame.result.invalid_market_state,
                    "reasons": frame.result.reasons,
                    "missing_opens": [at.isoformat() for at in frame.quality.missing_opens],
                    "unexpected_opens": [at.isoformat() for at in frame.quality.unexpected_opens],
                    "candles": [c.to_json_dict() for c in frame.result.candles],
                }
            )
        )


if __name__ == "__main__":
    main()
