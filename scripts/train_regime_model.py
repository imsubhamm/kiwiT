"""Train and report a research-only regime baseline from a KIW-14 artifact."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from kiwit.ml.regime import RegimeConfig, evaluate_walk_forward, train_regime


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dataset",
        type=Path,
        action="append",
        required=True,
        help="Repeat in chronological order for non-overlapping walk-forward test windows",
    )
    parser.add_argument("--output", type=Path, default=ROOT / "data/local/regime-models")
    parser.add_argument("--rounds", type=int, default=40)
    args = parser.parse_args()
    datasets = [json.loads(path.read_text()) for path in args.dataset]
    config = RegimeConfig(rounds=args.rounds)
    if len(datasets) > 1:
        report = evaluate_walk_forward(datasets, config=config, output=args.output)
        args.output.mkdir(parents=True, exist_ok=True)
        from kiwit.ml.datasets import _hash

        report_path = args.output / f"walk-forward-{_hash(report)}.json"
        report_path.write_text(json.dumps(report, sort_keys=True, indent=2) + "\n")
        print(json.dumps({"report": str(report_path), "status": "RESEARCH_ONLY"}))
        return
    model = train_regime(datasets[0], config=config)
    path = model.save(args.output)
    print(
        json.dumps(
            {"artifact": str(path), "test": model.artifact["evaluation"]["test"], "status": "RESEARCH_ONLY"}, indent=2
        )
    )


if __name__ == "__main__":
    main()
