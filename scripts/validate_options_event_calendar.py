#!/usr/bin/env python3
"""Validate the production options event calendar before workers start."""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

from kiwit.options_events import event_context


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", type=Path, required=True)
    parser.add_argument("--at", help="Optional timezone-aware ISO timestamp used by tests and drills")
    args = parser.parse_args()
    now = datetime.fromisoformat(args.at) if args.at else datetime.now().astimezone()
    context = event_context(now, args.path)
    print(json.dumps(context, sort_keys=True))
    return 0 if context.get("coverage") == "configured" else 1


if __name__ == "__main__":
    raise SystemExit(main())
