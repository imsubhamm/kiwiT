"""Frozen, paired option-quote comparisons; no model calls or invented prices.

Fixed-horizon counterfactuals are opportunity studies, not a portfolio backtest.
Only observations available after a decision may supply executable prices.
"""
from datetime import datetime, timedelta
from decimal import Decimal as D

from .intraday import IST
from .options_risk import fees, fill_price
from .playbooks import VERSION, fingerprint


def stamp(value):
    parsed = datetime.fromisoformat(str(value))
    if parsed.tzinfo is None:
        raise ValueError('Evidence timestamps must include a timezone')
    return parsed


def quotes(tape, symbol, start, end):
    result = []
    for row in tape:
        available = stamp(row['recorded_at'])
        if not start <= available <= end:
            continue
        for contract in row['market_snapshot'].get('candidates', []):
            if contract['symbol'] != symbol:
                continue
            quote = contract['quote']
            quoted = stamp(quote['stamp'])
            if not 0 <= (available - quoted).total_seconds() <= 90 or quoted < start:
                continue
            result.append((available, contract, row['market_snapshot']))
    return sorted(result, key=lambda pair: pair[0])


def counterfactual(plan, tape, at, horizon_minutes, extra_bps=0):
    if fingerprint({k: v for k, v in plan.items() if k != 'id'}) != plan['id']:
        return {'status': 'excluded', 'reason': 'plan_integrity'}
    deadline = min(at + timedelta(seconds=90), stamp(plan['expires_at']))
    for entered_at, contract, market in quotes(tape, plan['symbol'], at, deadline):
        if entered_at >= stamp(plan['expires_at']) or entered_at < stamp(plan['created_at']):
            continue
        if contract['kind'] != plan['kind'] or contract['expiry'] <= str(entered_at.astimezone(IST).date()):
            continue
        if not market.get('spot_at') or not 0 <= (entered_at - stamp(market['spot_at'])).total_seconds() <= 180:
            continue
        spot = D(str(market.get('spot', 'NaN')))
        trigger, invalidation, chase = (D(str(plan[k])) for k in
                                        ('underlying_trigger', 'underlying_invalidation', 'underlying_max_chase'))
        if not spot.is_finite() or not (invalidation < trigger <= spot <= chase if plan['kind'] == 'CE'
                                       else chase <= spot <= trigger < invalidation):
            continue
        quote = contract['quote']
        quantity = plan['quantity']
        if (quantity <= 0 or quantity % contract['lot'] or quantity >= contract['freeze']
                or quantity > min(quote['ask_size'], quote['bid_size'])):
            continue
        ask, bid = D(quote['ask']), D(quote['bid'])
        if not bid.is_finite() or not ask.is_finite() or not 0 < bid <= ask or (ask - bid) / ask > D('.02'):
            continue
        entry = fill_price(quote, contract, True)
        if entry > D(plan['max_fill']):
            continue
        due = entered_at + timedelta(minutes=horizon_minutes)
        for exited_at, exit_contract, _market in quotes(tape, plan['symbol'], due, due + timedelta(seconds=90)):
            local = exited_at.astimezone(IST)
            if local.date() != entered_at.astimezone(IST).date() or (local.hour, local.minute) >= (15, 30):
                continue
            if any(contract[key] != exit_contract[key] for key in ('expiry', 'lot', 'tick', 'kind', 'strike')):
                continue
            exit_bid = D(exit_contract['quote']['bid'])
            if exit_contract['quote']['bid_size'] < quantity or not exit_bid.is_finite() or exit_bid <= 0:
                continue
            exit_price = fill_price(exit_contract['quote'], contract, False)
            bought, sold = entry * quantity, exit_price * quantity
            baseline_fees = fees(bought) + fees(sold)
            stress_cost = (bought + sold) * D(extra_bps) / 10000
            return {'status': 'observed', 'entered_at': entered_at.isoformat(), 'exited_at': exited_at.isoformat(),
                    'quantity': quantity, 'gross_pnl': str(sold-bought),
                    'illustrative_fees': str(baseline_fees), 'extra_cost': str(stress_cost),
                    'net_pnl': str(sold-bought-baseline_fees-stress_cost)}
        return {'status': 'excluded', 'reason': 'future_exit_quote_or_depth_missing'}
    return {'status': 'excluded', 'reason': 'future_entry_quote_or_depth_missing'}


def compare(bundle, *, horizon_minutes=15, extra_bps=0):
    if not 1 <= horizon_minutes <= 60 or not 0 <= extra_bps <= 100:
        raise ValueError('Horizon 1–60 minutes and extra cost 0–100 bps required')
    rows = []
    for call in bundle['calls']:
        snapshot = call['snapshot']
        selection = snapshot.get('strategy_selection', {})
        plans = selection.get('plans', [])
        decision = (call.get('result') or {}).get('decision')
        if selection.get('version') != VERSION or not plans or snapshot.get('position'):
            continue
        if not decision or call['state'] not in ('applied', 'rejected'):
            continue
        # Start at response/application availability, not the pre-inference snapshot.
        at_value = (call.get('result') or {}).get('settled_at')
        if not at_value:
            rows.append({'call_id': str(call['call_id']), 'status': 'excluded', 'reason': 'response_time_missing'})
            continue
        at = stamp(at_value)
        if decision['action'] not in ('HOLD', 'BUY'):
            continue
        chosen = next((p for p in plans if p['id'] == decision.get('plan_id')), None)
        if decision['action'] == 'BUY' and chosen is None:
            rows.append({'call_id': str(call['call_id']), 'status': 'excluded', 'reason': 'unknown_selected_plan'})
            continue
        baseline = counterfactual(plans[0], bundle.get('market_tape', []), at, horizon_minutes, extra_bps)
        ai = (counterfactual(chosen, bundle.get('market_tape', []), at, horizon_minutes, extra_bps)
              if chosen else {'status': 'observed', 'net_pnl': '0', 'action': 'HOLD'})
        paired = baseline['status'] == ai['status'] == 'observed'
        rows.append({'call_id': str(call['call_id']), 'experiment_id': snapshot.get('experiment_id', 'unbound'),
                     'status': 'paired' if paired else 'excluded', 'ai': ai, 'first_eligible_plan': baseline,
                     'no_trade_net_pnl': '0'})
    groups = {}
    for row in rows:
        if row['status'] != 'paired':
            continue
        group = groups.setdefault(row['experiment_id'], {'pairs': 0, 'ai': D(0), 'baseline': D(0)})
        group['pairs'] += 1
        group['ai'] += D(row['ai']['net_pnl'])
        group['baseline'] += D(row['first_eligible_plan']['net_pnl'])
    return {'format': 'options-paired-opportunities-v1', 'horizon_minutes': horizon_minutes,
            'extra_cost_bps': extra_bps, 'comparisons': rows,
            'experiments': [{**g, 'ai': str(g['ai']), 'baseline': str(g['baseline']),
                             'difference': str(g['ai']-g['baseline']), 'experiment_id': key} for key, g in groups.items()],
            'profitability_validated': False,
            'limitations': ['Overlapping fixed-horizon opportunities; not portfolio returns',
                            'Costs are illustrative plus stated stress; require broker cost reconciliation',
                            'Quote/depth gaps excluded; report exclusions before interpreting paired samples']}


def session_acceptance(bundle, day, release):
    """Read-only post-deployment evidence; a quiet day never claims a completed fill chain."""
    events = [e for e in bundle['events'] if str(e['trading_date']) == day]
    calls = [c for c in bundle['calls'] if str(c['trading_date']) == day
             and c['snapshot'].get('provenance', {}).get('release') == release]
    observations = [r for r in bundle.get('market_tape', []) if str(r['trading_date']) == day
                    and r['market_snapshot'].get('provenance', {}).get('release') == release]
    ids = {str(c['call_id']) for c in calls if c['state'] in ('applied', 'rejected')
           and (c.get('result') or {}).get('decision')}
    entries = [e for e in events if e['kind'] == 'paper_entry' and str(e['detail']['call_id']) in ids]
    positions = {e['detail']['position']['id'] for e in entries}
    exits = [e for e in events if e['kind'] == 'paper_exit' and e['detail'].get('position_id') in positions
             and e['detail'].get('closed')]
    reports = [r for r in bundle.get('reports', []) if str(r['trading_date']) == day]
    closed = {e['detail']['position_id'] for e in exits}
    flat_reports = [r for r in reports if r['report'].get('reconciled_flat')]
    checks = {'observation': bool(observations), 'decision': bool(ids), 'entry': bool(entries),
              'exit': bool(positions) and positions <= closed, 'flat_report': bool(flat_reports),
              'report_delivery': any(r['delivery_status'] == 'sent' for r in flat_reports)}
    return {'day': day, 'release': release, 'checks': checks,
            'status': 'complete' if all(checks.values()) else 'incomplete',
            'missing': [key for key, ok in checks.items() if not ok],
            'note': 'Do not force a trade to satisfy this evidence gate'}
