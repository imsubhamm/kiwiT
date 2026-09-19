import argparse
import json
from pathlib import Path

from kiwit.options_replay import replay


def main():
    parser = argparse.ArgumentParser(description='Replay the deployed options selector on frozen recorded inputs')
    parser.add_argument('bundle', type=Path)
    args = parser.parse_args()
    result = replay(json.loads(args.bundle.read_text()))
    print(json.dumps(result, indent=2))
    raise SystemExit(1 if result['mismatches'] else 0)


if __name__ == '__main__':
    main()
