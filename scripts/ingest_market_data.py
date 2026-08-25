from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from kiwit.marketdata.pipeline import MarketDataPipeline


parser = argparse.ArgumentParser()
parser.add_argument("--start", type=date.fromisoformat, required=True)
parser.add_argument("--end", type=date.fromisoformat, required=True)
parser.add_argument("--root", type=Path, default=ROOT / "data" / "market")
parser.add_argument("--workers", type=int, default=10)
args = parser.parse_args()
print(json.dumps(MarketDataPipeline(args.root).ingest_range(args.start, args.end, workers=args.workers), indent=2))
