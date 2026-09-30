"""Frozen, paired option-quote comparisons; no model calls or invented prices.

Fixed-horizon counterfactuals are opportunity studies, not a portfolio backtest.
Only observations available after a decision may supply executable prices.
"""
from datetime import datetime, timedelta
from decimal import Decimal as D

from .intraday import IST
from .options_retention import ENTRY_DELAY_SECONDS, MAX_HORIZON_MINUTES, QUOTE_GRACE_SECONDS
from .options_risk import BROKER_COST_VERSION, fees, fill_price
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
    deadline = min(at + timedelta(seconds=ENTRY_DELAY_SECONDS), stamp(plan['expires_at']))
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
        for exited_at, exit_contract, _market in quotes(tape, plan['symbol'], due, due + timedelta(seconds=QUOTE_GRACE_SECONDS)):
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
            baseline_fees = fees(bought, "buy") + fees(sold, "sell")
            stress_cost = (bought + sold) * D(extra_bps) / 10000
            return {'status': 'observed', 'entered_at': entered_at.isoformat(), 'exited_at': exited_at.isoformat(),
                    'quantity': quantity, 'gross_pnl': str(sold-bought),
                    'broker_costs': str(baseline_fees), 'cost_model': BROKER_COST_VERSION,
                    'extra_cost': str(stress_cost),
                    'net_pnl': str(sold-bought-baseline_fees-stress_cost)}
        return {'status': 'excluded', 'reason': 'future_exit_quote_or_depth_missing'}
    return {'status': 'excluded', 'reason': 'future_entry_quote_or_depth_missing'}


def compare(bundle, *, horizon_minutes=15, extra_bps=0):
    if not 1 <= horizon_minutes <= MAX_HORIZON_MINUTES or not 0 <= extra_bps <= 100:
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
            'cost_model': BROKER_COST_VERSION,
            'limitations': ['Overlapping fixed-horizon opportunities; not portfolio returns',
                            'Cost schedule is versioned; contract-note reconciliation remains authoritative',
                            'Quote/depth gaps excluded; report exclusions before interpreting paired samples']}


def _measurable_opportunities(bundle):
    """One occurrence per selection time and frozen plan, preferring scan evidence.

    A later scan of the same plan is a separate observation. A paid call copying
    that selection is not. These records never confer execution authority.
    """
    # Legacy scans did not retain capital. Recover only from the exact frozen
    # selection in a snapshot, never from a later session balance.
    contexts = {}
    snapshots = [(c.get('snapshot') or {}, c.get('trading_date'), 'ai_snapshot')
                 for c in bundle.get('calls', [])]
    snapshots += [(r.get('market_snapshot') or {}, r.get('trading_date'), 'market_snapshot')
                  for r in bundle.get('market_tape', [])]
    for snapshot, day, source in snapshots:
        selection = snapshot.get('strategy_selection') or {}
        if snapshot.get('capital') is None:
            continue
        for plan in selection.get('plans', []) + selection.get('shadow_plans', []):
            key = (str(day), selection.get('at'), plan['id'])
            contexts[key] = {'capital': snapshot['capital'], 'source': source}
    records = []
    for event in bundle.get('events', []):
        if event.get('kind') != 'strategy_scan':
            continue
        detail = event.get('detail') or {}
        for field, source in (('plans', 'strategy_scan'), ('shadow_plans', 'calendar_shadow')):
            for plan in detail.get(field, []):
                records.append((detail, plan, event.get('event_at'), event.get('trading_date'),
                                detail.get('measurement_context') or plan.get('measurement_context') or
                                contexts.get((str(event.get('trading_date')), detail.get('at'), plan['id']), {}),
                                source, str(event.get('event_id', ''))))
    for call in bundle.get('calls', []):
        snapshot = call.get('snapshot') or {}
        selection = snapshot.get('strategy_selection') or {}
        for plan in selection.get('plans', []):
            records.append(({**selection, 'entry_gate': snapshot.get('entry_gate', {}),
                             'provenance': snapshot.get('provenance', {})}, plan,
                            (call.get('result') or {}).get('settled_at'), call.get('trading_date'),
                            {'capital': snapshot.get('capital')}, 'ai_call', str(call.get('call_id', ''))))
    seen = set()
    for selection, plan, available, day, context, source, source_id in records:
        occurrence_at = selection.get('at') or (available if source != 'ai_call' else None) or plan.get('created_at')
        identity = fingerprint({'day': str(day), 'at': occurrence_at, 'plan_id': plan['id']})
        if identity in seen:
            continue
        seen.add(identity)
        reason = None
        try:
            at = stamp(available)
        except (ValueError, TypeError):
            at = None
            reason = 'availability_time_missing_or_invalid'
        try:
            capital = D(str(context.get('capital')))
            if not capital.is_finite() or capital <= 0:
                raise ValueError('Invalid capital')
        except (ArithmeticError, ValueError):
            capital = None
            reason = reason or 'capital_missing_or_invalid'
        gate = selection.get('entry_gate') or {}
        yield {'occurrence_id': identity, 'at': at, 'day': str(day), 'capital': capital,
               'plan': plan, 'source': source, 'source_id': source_id,
               'available_at': available, 'selection_at': occurrence_at,
               'capital_source': context.get('source', source),
               'entry_gate': gate, 'provenance': selection.get('provenance', {}),
               'block_reason_codes': sorted(set(gate.get('reason_codes', []) + plan.get('block_reason_codes', []))),
               'evidence_only': True, 'execution_eligible': False, 'excluded_reason': reason}


def _opportunity_outcome(item, bundle, horizon):
    if item['excluded_reason']:
        return {'status': 'excluded', 'reason': item['excluded_reason']}
    return counterfactual(item['plan'], bundle.get('market_tape', []), item['at'], horizon)


def _opportunity_evidence(item, outcome):
    return {key: value for key, value in item.items() if key not in ('at', 'capital', 'plan', 'excluded_reason')} | {
        'plan_id': item['plan']['id'], 'playbook_id': item['plan']['playbook_id'], 'outcome': outcome}


def rule_matrix(bundle, *, horizon_minutes=15):
    """Apply cap/loss scenarios to the same observed opportunity stream."""
    if not 1 <= horizon_minutes <= MAX_HORIZON_MINUTES:
        raise ValueError('Horizon 1–60 minutes required')
    opportunities, evidence = [], []
    for item in _measurable_opportunities(bundle):
        observed = _opportunity_outcome(item, bundle, horizon_minutes)
        evidence.append(_opportunity_evidence(item, observed))
        if observed['status'] == 'observed':
            opportunities.append({**item, 'net_pnl': D(observed['net_pnl'])})
    opportunities.sort(key=lambda row: (row['at'], row['occurrence_id']))
    rows = []
    for cap in (4, 6, 10):
        for loss_pct in (2, 3, 5):
            daily, selected, exclusions = {}, [], []
            for row in evidence:
                if row['outcome']['status'] != 'observed':
                    exclusions.append({'occurrence_id': row['occurrence_id'], 'reason': row['outcome']['reason']})
            for item in opportunities:
                state = daily.setdefault(item['day'], {'entries': 0, 'pnl': D(0)})
                reason = ('scenario_entry_cap' if state['entries'] >= cap else
                          'scenario_daily_loss' if state['pnl'] <= -item['capital'] * D(loss_pct) / 100 else None)
                if reason:
                    exclusions.append({'occurrence_id': item['occurrence_id'], 'reason': reason})
                    continue
                state['entries'] += 1
                state['pnl'] += item['net_pnl']
                selected.append(item)
            rows.append({'entry_cap': cap, 'daily_loss_pct': loss_pct, 'observed_opportunities': len(selected),
                         'attempted': len(evidence), 'included': len(selected), 'excluded': len(exclusions),
                         'exclusions': exclusions,
                         'net_pnl': str(sum((item['net_pnl'] for item in selected), D(0)))})
    return {'format': 'options-rule-matrix-v1', 'horizon_minutes': horizon_minutes,
            'attempted': len(evidence), 'included': len(opportunities),
            'excluded': len(evidence) - len(opportunities), 'opportunities': evidence,
            'observed_opportunities': len(opportunities), 'scenarios': rows, 'promotion_eligible': False,
            'limitations': ['Sequential fixed-horizon opportunities may overlap; this compares rules, not portfolio returns',
                            'Missing contract quotes remain excluded and are never imputed']}


def exit_matrix(bundle):
    """Compare playbook-specific holding horizons on identical retained quotes."""
    groups, evidence = {}, []
    total = excluded = 0
    for item in _measurable_opportunities(bundle):
        plan = item['plan']
        for horizon in plan.get('exit_experiments', {}).get('max_hold_minutes', []):
            total += 1
            observed = _opportunity_outcome(item, bundle, horizon)
            evidence.append({**_opportunity_evidence(item, observed), 'horizon_minutes': horizon})
            key = (plan['playbook_id'], horizon)
            group = groups.setdefault(key, {'playbook_id': key[0], 'horizon_minutes': horizon,
                                            'attempted': 0, 'included': 0, 'excluded': 0,
                                            'exclusion_reasons': {}, 'observations': 0, 'net_pnl': D(0)})
            group['attempted'] += 1
            if observed['status'] != 'observed':
                excluded += 1
                group['excluded'] += 1
                reasons = group['exclusion_reasons']
                reasons[observed['reason']] = reasons.get(observed['reason'], 0) + 1
                continue
            group['included'] += 1
            group['observations'] += 1
            group['net_pnl'] += D(observed['net_pnl'])
    rows = [{**value, 'net_pnl': str(value['net_pnl'])} for value in groups.values()]
    return {'format': 'options-exit-matrix-v1', 'attempted': total, 'included': total - excluded,
            'excluded': excluded, 'scenarios': rows, 'opportunities': evidence, 'promotion_eligible': False,
            'limitations': ['Overlapping fixed-horizon opportunity studies; not portfolio returns']}


def cost_reconciliation(bundle):
    estimated = {}
    for event in bundle.get('events', []):
        detail = event.get('detail') or {}
        if event.get('kind') == 'paper_entry':
            position_id = (detail.get('position') or {}).get('id')
            costs = detail.get('entry_costs') or {}
        elif event.get('kind') == 'paper_exit':
            position_id = detail.get('position_id')
            costs = detail.get('exit_costs') or {}
        else:
            continue
        if position_id and costs.get('total') is not None:
            estimated[position_id] = estimated.get(position_id, D(0)) + D(str(costs['total']))
    actual = {}
    for row in bundle.get('broker_cost_evidence', []):
        actual[row['position_id']] = actual.get(row['position_id'], D(0)) + D(str(row['actual_cost']))
    rows = []
    for position_id in sorted(set(estimated) | set(actual)):
        estimate = estimated.get(position_id)
        invoice = actual.get(position_id)
        rows.append({'position_id': position_id, 'estimated_cost': str(estimate) if estimate is not None else None,
                     'actual_cost': str(invoice) if invoice is not None else None,
                     'difference': str(estimate-invoice) if estimate is not None and invoice is not None else None,
                     'status': 'reconciled' if estimate is not None and invoice is not None else 'missing_evidence'})
    return {'format': 'options-cost-reconciliation-v1', 'cost_model': BROKER_COST_VERSION, 'positions': rows,
            'reconciled': sum(row['status'] == 'reconciled' for row in rows)}


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
