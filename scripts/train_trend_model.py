"""Train a research-only trend opportunity model from a KIW-14 dataset."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from kiwit.ml.trend import TrendConfig, train_trend


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=ROOT / "data/local/trend-models")
    parser.add_argument("--rounds", type=int, default=40)
    parser.add_argument("--round-trip-cost", type=float, default=0.0002)
    parser.add_argument("--round-trip-slippage", type=float, default=0.0004)
    args = parser.parse_args()
    model = train_trend(
        json.loads(args.dataset.read_text()),
        config=TrendConfig(
            rounds=args.rounds,
            round_trip_cost=args.round_trip_cost,
            round_trip_slippage=args.round_trip_slippage,
        ),
    )
    path = model.save(args.output)
    print(
        json.dumps(
            {"artifact": str(path), "test": model.artifact["evaluation"]["test"], "status": "RESEARCH_ONLY"}, indent=2
        )
    )


if __name__ == "__main__":
    main()
