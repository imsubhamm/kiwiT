"""Deterministic replay parity for the deployed selector, never invented option P&L.

This consumes recorded snapshots and frozen decisions, with no model/network calls.
Held-out performance requires a complete executable option tape and remains distinct.
"""
from datetime import datetime
from decimal import Decimal as D

from .options_risk import fees, fill_price, trade_limits
from .playbooks import VERSION, fingerprint, select_plans, validate_plan


def replay_fills(bundle):
    """Recheck frozen entry authority and fill accounting, including partial exits.

    This cannot reconstruct historical external halt state or operator consent.
    Missing evidence stays unreplayable rather than being counted as a match.
    """
    calls = {str(call['call_id']): call for call in bundle['calls']}
    positions, results = {}, []
    for event in bundle.get('events', []):
        kind, detail = event['kind'], event['detail']
        if kind not in ('paper_entry', 'paper_exit', 'paper_settlement'):
            continue
        status = 'match'
        try:
            if kind == 'paper_entry':
                position = detail['position']
                call = calls[detail['call_id']]
                snapshot = call['snapshot']
                state = {**snapshot, 'amount': snapshot['capital']}
                contract = next(c for c in snapshot['candidates'] if c['symbol'] == position['contract']['symbol'])
                plan = validate_plan(call['result']['decision'], snapshot, state, detail['quote'],
                                     position['entry_underlying'], datetime.fromisoformat(position['entered_at']))
                price = fill_price(detail['quote'], contract, True)
                cost = price * plan['quantity'] + fees(price * plan['quantity'])
                checks = (plan == position['entry_plan'], position['quantity'] == plan['quantity'],
                          position['contract'] == {k: v for k, v in contract.items() if k != 'quote'},
                          D(position['entry']) == price, D(position['entry_cost_remaining']) == cost,
                          D(position['stop']) == price * (1 - trade_limits(state)[0] / 100),
                          D(position['target']) == price * (1 + trade_limits(state)[1] / 100))
                if not all(checks):
                    status = 'mismatch'
                else:
                    positions[position['id']] = {'contract': contract, 'quantity': plan['quantity'], 'cost': cost}
            else:
                position = positions[detail['position_id']]
                quantity = detail['quantity']
                contract = position['contract']
                if kind == 'paper_exit':
                    price = fill_price(detail['quote'], contract, False)
                    expected_qty = min(position['quantity'], detail['quote']['bid_size']) // contract['lot'] * contract['lot']
                    costs = fees(price * quantity)
                    if quantity != expected_qty or D(detail['price']) != price:
                        status = 'mismatch'
                else:
                    price, costs = D(detail['settlement_price']), D(detail['settlement_fees'])
                    if quantity != position['quantity']:
                        status = 'mismatch'
                if not 0 < quantity <= position['quantity'] or quantity % contract['lot']:
                    raise ValueError('Invalid exit quantity')
                basis = position['cost'] * D(quantity) / position['quantity']
                pnl = price * quantity - costs - basis
                if kind == 'paper_exit':
                    pnl = pnl.quantize(D('.00000001'))
                if D(detail['pnl']) != pnl or detail['closed'] != (quantity == position['quantity']):
                    status = 'mismatch'
                position['quantity'] -= quantity
                position['cost'] -= basis
        except (KeyError, StopIteration):
            status = 'unreplayable'
        except (ValueError, ArithmeticError, TypeError):
            status = 'mismatch'
        results.append({'event_id': str(event.get('event_id', 'unknown')), 'kind': kind, 'status': status})
    return results


def _canonical_plans(plans):
    """Require every frozen plan field and its content digest to match."""
    canonical = []
    for plan in plans:
        body = {key: value for key, value in plan.items() if key != 'id'}
        canonical.append({**body, 'id': fingerprint(body)})
    return canonical


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
        expected = _canonical_plans(selection['plans'])
        actual = _canonical_plans(rebuilt['plans'])
        status = 'match' if actual == expected and selection['plans'] == expected else 'mismatch'
        results.append({'call_id': str(call['call_id']), 'status': status,
                        'expected_plan_ids': [p['id'] for p in expected],
                        'actual_plan_ids': [p['id'] for p in actual]})
    return {'format': 'banknifty-replay-parity-v1', 'calls': results,
            'fills': replay_fills(bundle),
            'limitations': ['External halt and consent history are not reconstructed',
                            'Settlement verifies recorded accounting, not operator source authenticity'],
            'matching': sum(r['status'] == 'match' for r in results),
            'mismatches': sum(r['status'] == 'mismatch' for r in results),
            'unreplayable': sum(r['status'] == 'legacy_unreplayable' for r in results),
            'profitability_validated': False}
