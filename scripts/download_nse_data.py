from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tradingkiwi.nse_data import download_dataset


parser = argparse.ArgumentParser()
parser.add_argument("--start", type=date.fromisoformat, default=date(2015, 1, 1))
parser.add_argument("--end", type=date.fromisoformat, default=date(2024, 7, 5))
parser.add_argument("--output", type=Path, default=Path("data/official_nse"))
parser.add_argument("--workers", type=int, default=8)
args = parser.parse_args()

paths = download_dataset(args.start, args.end, args.output, args.workers)
print("\n".join(str(path) for path in paths))

