"""Deterministic replay parity for the deployed selector, never invented option P&L.

This consumes recorded snapshots and frozen decisions, with no model/network calls.
Held-out performance requires a complete executable option tape and remains distinct.
"""
from datetime import datetime

from .playbooks import VERSION, select_plans


def replay(bundle):
    results = []
    for call in bundle['calls']:
        snapshot = call['snapshot']
        selection = snapshot.get('strategy_selection', {})
        if selection.get('version') != VERSION or not snapshot.get('provenance'):
            results.append({'call_id': str(call['call_id']), 'status': 'legacy_unreplayable'})
            continue
        state = {**snapshot, 'amount': snapshot['capital']}
        rebuilt = select_plans(snapshot, state, datetime.fromisoformat(selection['at']))
        expected = [p['id'] for p in selection['plans']]
        actual = [p['id'] for p in rebuilt['plans']]
        results.append({'call_id': str(call['call_id']), 'status': 'match' if actual == expected else 'mismatch',
                        'expected_plans': expected, 'actual_plans': actual})
    return {'format': 'banknifty-replay-parity-v1', 'calls': results,
            'matching': sum(r['status'] == 'match' for r in results),
            'mismatches': sum(r['status'] == 'mismatch' for r in results),
            'unreplayable': sum(r['status'] == 'legacy_unreplayable' for r in results),
            'profitability_validated': False}
