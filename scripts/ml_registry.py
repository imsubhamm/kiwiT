"""Train, activate and roll back version-bound local research models."""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from kiwit.ml.framework import ModelRegistry, TrainingRun


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", type=Path, required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    train = commands.add_parser("train")
    train.add_argument("--config", type=Path, required=True)
    train.add_argument("--dataset", type=Path, required=True)
    activate = commands.add_parser("activate")
    activate.add_argument("--kind", required=True)
    activate.add_argument("--fingerprint", required=True)
    rollback = commands.add_parser("rollback")
    rollback.add_argument("--kind", required=True)
    args = parser.parse_args()
    registry = ModelRegistry(args.registry)
    if args.command == "train":
        result = registry.train_and_register(
            TrainingRun(**json.loads(args.config.read_text())), json.loads(args.dataset.read_text())
        )
    elif args.command == "activate":
        result = registry.activate(args.kind, args.fingerprint)
    else:
        result = registry.rollback(args.kind)
    print(json.dumps({"result": result, "status": "RESEARCH_ONLY"}))


if __name__ == "__main__":
    main()
