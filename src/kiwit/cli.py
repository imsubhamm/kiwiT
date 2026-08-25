from __future__ import annotations

import argparse
import json
from dataclasses import asdict

from . import __version__
from .config import load_config


def main() -> None:
    parser = argparse.ArgumentParser(prog="kiwit")
    parser.add_argument("--version", action="store_true")
    parser.add_argument("--config", default="config/kiwit.toml")
    parser.add_argument("command", nargs="?", choices=["doctor"])
    args = parser.parse_args()
    if args.version:
        print(__version__)
        return
    if args.command == "doctor":
        config = load_config(args.config)
        print(json.dumps({"status": "ok", "name": config.name, "environment": config.environment, "live_execution_enabled": config.live_execution_enabled}, default=str))
        return
    parser.print_help()


if __name__ == "__main__":
    main()
