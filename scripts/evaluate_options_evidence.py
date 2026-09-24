"""Offline paired comparisons and release acceptance from an exported evidence bundle."""
import argparse
import json
from pathlib import Path

from kiwit.options_evaluation import compare, cost_reconciliation, exit_matrix, rule_matrix, session_acceptance


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('bundle', type=Path)
    parser.add_argument('--day')
    parser.add_argument('--release')
    parser.add_argument('--horizon-minutes', type=int, default=15)
    parser.add_argument('--extra-cost-bps', type=int, default=0)
    args = parser.parse_args()
    bundle = json.loads(args.bundle.read_text())
    result = compare(bundle, horizon_minutes=args.horizon_minutes, extra_bps=args.extra_cost_bps)
    result['rule_matrix'] = rule_matrix(bundle, horizon_minutes=args.horizon_minutes)
    result['exit_matrix'] = exit_matrix(bundle)
    result['cost_reconciliation'] = cost_reconciliation(bundle)
    if bool(args.day) != bool(args.release):
        parser.error('--day and --release must be supplied together')
    if args.day:
        result['session_acceptance'] = session_acceptance(bundle, args.day, args.release)
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
