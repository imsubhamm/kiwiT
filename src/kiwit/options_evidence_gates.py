"""Offline evidence for KIW-55 through KIW-58. No orders and no ledger writes.

A pass means the requested evidence is complete. It does not authorize promotion
or live trading. Missing tape, notes, or session stages stay incomplete.
"""
from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import time, timedelta
from decimal import Decimal as D

from .banknifty import fresh, market_window
from .intraday import IST
from .options_evaluation import quotes, stamp
from .options_policy import COOLDOWN_SECONDS, ENTRY_CAP
from .options_replay import replay
from .options_risk import BROKER_COST_VERSION, cost_breakdown, exit_levels, fees, fill_price
from .playbooks import underlying_exit

EXIT_REASONS = ('session_stop', 'exit_pending', 'stop_loss', 'take_profit', 'underlying_invalidation', 'playbook_time_exit')
DETERMINISTIC_REASONS = set(EXIT_REASONS)
PAISA = D('0.01')
COST_COMPONENTS = ('brokerage', 'exchange', 'ipft', 'sebi', 'gst', 'stamp', 'stt', 'total')
FORBIDDEN_NOTE_FIELDS = frozenset({'pan', 'account', 'account_number', 'client_id', 'ucc', 'demat', 'client_name'})
BASELINE_SCENARIOS = (
    {'name': 'production', 'entry_cap': ENTRY_CAP, 'cooldown_seconds': COOLDOWN_SECONDS, 'event_window': 'enforce'},
    {'name': 'cap_4', 'entry_cap': 4, 'cooldown_seconds': COOLDOWN_SECONDS, 'event_window': 'enforce'},
    {'name': 'cap_6', 'entry_cap': 6, 'cooldown_seconds': COOLDOWN_SECONDS, 'event_window': 'enforce'},
    {'name': 'cooldown_off', 'entry_cap': ENTRY_CAP, 'cooldown_seconds': 0, 'event_window': 'enforce'},
    {'name': 'event_window_ignored', 'entry_cap': ENTRY_CAP, 'cooldown_seconds': COOLDOWN_SECONDS, 'event_window': 'ignore'},
)
POLICIES = ('ai', 'deterministic', 'hold')


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), default=str)


def _sha(value):
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _money(value):
    return str(value) if value is not None else None


def _decimal(value):
    number = D(str(value))
    if not number.is_finite():
        raise ValueError('non-finite')
    return number


def _interval(values):
    count = len(values)
    if not count:
        return None, None, None
    mean = sum(values, D(0)) / count
    if count < 2:
        return mean, None, None
    variance = sum((item - mean) ** 2 for item in values) / (count - 1)
    margin = variance.sqrt() / D(count).sqrt() * D('1.96')
    return mean, mean - margin, mean + margin


def _contract_for(position, bundle):
    symbol = (position.get('contract') or {}).get('symbol')
    for call in bundle.get('calls') or []:
        for candidate in (call.get('snapshot') or {}).get('candidates') or []:
            if candidate.get('symbol') == symbol:
                return candidate
    contract = position.get('contract') or {}
    if {'lot', 'tick', 'symbol'} <= set(contract):
        return contract
    return None


def _exit_reason(position, price, underlying, now):
    local = now.astimezone(IST)
    if position.get('day') and position['day'] != str(local.date()) or local.time() >= time(15, 15):
        return 'session_stop'
    if position.get('exit_pending'):
        return position['exit_pending']
    if price <= D(position['stop']):
        return 'stop_loss'
    if price >= D(position['target']):
        return 'take_profit'
    if underlying_exit(position, underlying, now):
        return 'underlying_invalidation'
    deadline = position.get('exit_deadline')
    if deadline and now >= stamp(deadline):
        return 'playbook_time_exit'
    return None


def _quote_rows(tape, symbol, start):
    rows = []
    for recorded_at, contract, market in quotes(tape, symbol, start, start + timedelta(days=2)):
        if recorded_at < start:
            continue
        rows.append((recorded_at, contract, market))
    return rows


def walk_exit(position, tape):
    """Replay one open paper position with the desk's exit order. No invented prices."""
    symbol = (position.get('contract') or {}).get('symbol')
    start = stamp(position['entered_at'])
    quantity = int(position['quantity'])
    lot = int(position['contract']['lot'])
    fills, gaps = [], []
    last_stamp = None
    for recorded_at, contract, market in _quote_rows(tape, symbol, start):
        if not market_window(recorded_at):
            gaps.append({'at': recorded_at.isoformat(), 'reason': 'outside_market_window'})
            continue
        quote = contract.get('quote') or {}
        if not fresh(quote, recorded_at):
            gaps.append({'at': recorded_at.isoformat(), 'reason': 'stale_or_missing_quote'})
            continue
        if quote.get('stamp') == last_stamp:
            continue
        if any(position['contract'].get(key) != contract.get(key) for key in ('expiry', 'lot', 'tick', 'kind', 'strike')):
            gaps.append({'at': recorded_at.isoformat(), 'reason': 'contract_identity_mismatch'})
            continue
        try:
            price = fill_price(quote, position['contract'], False)
        except (ArithmeticError, KeyError, TypeError, ValueError):
            gaps.append({'at': recorded_at.isoformat(), 'reason': 'unusable_quote'})
            continue
        if price <= 0:
            gaps.append({'at': recorded_at.isoformat(), 'reason': 'non_positive_price'})
            continue
        underlying = {'at': market.get('spot_at'), 'spot': market.get('spot')} if market.get('spot_at') else None
        reason = _exit_reason(position, price, underlying, recorded_at)
        if reason is None:
            continue
        fill_qty = min(quantity, int(quote.get('bid_size') or 0)) // lot * lot
        if fill_qty <= 0:
            gaps.append({'at': recorded_at.isoformat(), 'reason': 'partial_liquidity'})
            continue
        fills.append({
            'reason': reason, 'price': str(price), 'quantity': fill_qty,
            'at': recorded_at.isoformat(), 'quote_stamp': quote.get('stamp'),
            'costs': {key: str(value) for key, value in cost_breakdown(price * fill_qty, 'sell').items() if key != 'sources'},
        })
        quantity -= fill_qty
        last_stamp = quote.get('stamp')
        position = {**position, 'quantity': quantity, 'exit_pending': reason}
        if quantity <= 0:
            break
    status = 'closed' if quantity <= 0 else 'missing_path'
    return {'status': status, 'fills': fills, 'gaps': gaps, 'residual_quantity': quantity}


def replay_exit_paths(bundle):
    """Compare reconstructed v5 exits with recorded paper exits."""
    tape = bundle.get('market_tape') or []
    recorded = {}
    entries = {}
    for event in bundle.get('events') or []:
        detail = event.get('detail') or {}
        if event.get('kind') == 'paper_entry':
            position = detail.get('position') or {}
            if position.get('id'):
                entries[str(position['id'])] = position
        elif event.get('kind') == 'paper_exit':
            recorded.setdefault(str(detail.get('position_id')), []).append(detail)
    rows, matched = [], 0
    for position_id, position in sorted(entries.items()):
        if position.get('exit_policy') != 'playbook_cost_aware_v5':
            rows.append({'position_id': position_id, 'status': 'excluded', 'reason': 'other_exit_policy'})
            continue
        contract = _contract_for(position, bundle)
        if contract is None:
            rows.append({'position_id': position_id, 'status': 'incomplete', 'reason': 'contract_missing'})
            continue
        walking = {**position, 'contract': {**contract, **(position.get('contract') or {})}}
        walked = walk_exit(walking, tape)
        actual = recorded.get(position_id) or []
        discrepancies = []
        if walked['status'] != 'closed':
            discrepancies.append('missing_path')
        if len(actual) != len(walked['fills']):
            discrepancies.append('fill_count')
        for expected, seen in zip(walked['fills'], actual, strict=False):
            if seen.get('reason') != expected['reason']:
                discrepancies.append('reason')
            if str(seen.get('price')) != expected['price']:
                discrepancies.append('price')
            if int(seen.get('quantity') or -1) != expected['quantity']:
                discrepancies.append('quantity')
            if (seen.get('quote') or {}).get('stamp') != expected['quote_stamp']:
                discrepancies.append('quote_stamp')
        if walked['status'] != 'closed':
            status = 'incomplete'
        elif not actual or discrepancies:
            status = 'mismatch'
        else:
            status = 'match'
        matched += status == 'match'
        rows.append({
            'position_id': position_id, 'status': status, 'replay': walked,
            'recorded_fills': len(actual), 'discrepancies': sorted(set(discrepancies)),
            'gap_count': len(walked['gaps']),
        })
    considered = [row for row in rows if row['status'] != 'excluded']
    if not considered or any(row['status'] == 'incomplete' for row in considered):
        status = 'incomplete'
    elif any(row['status'] == 'mismatch' for row in considered):
        status = 'fail'
    else:
        status = 'pass'
    return {
        'format': 'options-exit-path-replay-v1', 'status': status, 'positions': rows,
        'matched': matched, 'attempted': len(considered),
        'coverage': _money(D(matched) / len(considered) if considered else None),
        'fixed_horizon_used': False, 'promotion_eligible': False, 'live_orders': 'disabled',
        'limitations': [
            'Paths use recorded quotes only.',
            'A halt-driven session stop is not reconstructed unless the quote clock is already at or after 15:15 IST.',
            'Fixed-horizon returns are not exit-policy validation.',
        ],
    }


def _ai_buys(bundle):
    chosen = set()
    for call in bundle.get('calls') or []:
        decision = (call.get('result') or {}).get('decision') or {}
        if decision.get('action') == 'BUY' and decision.get('plan_id'):
            chosen.add(decision['plan_id'])
    return chosen


def _enter(item, cash, realized):
    plan = item['plan']
    if item.get('excluded_reason'):
        return None, item['excluded_reason']
    if plan.get('exit_policy') != 'playbook_cost_aware_v5' or not plan.get('live_exit'):
        return None, 'exit_policy_missing'
    at = item.get('at')
    if at is None:
        return None, 'availability_time_missing_or_invalid'
    for recorded_at, contract, market in quotes(item['tape'], plan['symbol'], at, min(at + timedelta(seconds=120), stamp(plan['expires_at']))):
        if recorded_at < stamp(plan['created_at']) or recorded_at >= stamp(plan['expires_at']):
            continue
        if contract.get('kind') != plan.get('kind'):
            continue
        quote = contract.get('quote') or {}
        try:
            price = fill_price(quote, contract, True)
            quantity = int(plan['quantity'])
            if quantity <= 0 or quantity % int(contract['lot']) or quantity > min(int(quote['ask_size']), int(quote['bid_size'])):
                continue
            if price > _decimal(plan['max_fill']):
                continue
            state = {
                'amount': str(item['capital']), 'cash': str(cash), 'realized_pnl': str(realized),
                'loss_pct': str(plan.get('loss_pct', '2')), 'profit_pct': str(plan.get('profit_pct', '3')),
                'trade_stop_pct': str(plan.get('loss_pct', '2')), 'trade_target_pct': str(plan.get('profit_pct', '3')),
            }
            levels = exit_levels(state, plan['live_exit'], price, quantity, contract)
        except (ArithmeticError, KeyError, TypeError, ValueError):
            continue
        cost = price * quantity + fees(price * quantity, 'buy')
        if cost > cash:
            return None, 'insufficient_cash'
        deadline = recorded_at + timedelta(minutes=int(levels['max_hold_minutes']))
        position = {
            'contract': contract, 'quantity': quantity, 'stop': str(levels['stop']), 'target': str(levels['target']),
            'entered_at': recorded_at.isoformat(), 'exit_deadline': deadline.isoformat(), 'entry_plan': plan,
            'exit_policy': 'playbook_cost_aware_v5', 'day': str(recorded_at.astimezone(IST).date()),
        }
        return {
            'position': position, 'price': price, 'quantity': quantity, 'cost': cost,
            'entry_costs': cost_breakdown(price * quantity, 'buy')['total'], 'at': recorded_at,
        }, None
    return None, 'future_entry_quote_or_depth_missing'


def _portfolio(opportunities, bundle, scenario, policy, bought):
    cash = next((item['capital'] for item in opportunities if item.get('capital')), None)
    if cash is None:
        return {'trades': [], 'vetoes': {}, 'exclusions': {'capital_missing_or_invalid': len(opportunities)},
                'exit_path_missing': 0, 'policy_holds': 0}
    realized = D(0)
    open_until = None
    last_exit = None
    entries = Counter()
    trades, vetoes, exclusions = [], Counter(), Counter()
    holds = 0
    ordered = sorted((item for item in opportunities if item.get('at')), key=lambda item: (item['at'], item['occurrence_id']))
    for item in opportunities:
        if item.get('at') is None:
            exclusions[item.get('excluded_reason') or 'availability_time_missing_or_invalid'] += 1
    for item in ordered:
        if item.get('excluded_reason'):
            exclusions[item['excluded_reason']] += 1
            continue
        if policy == 'hold' or (policy == 'ai' and item['plan']['id'] not in bought):
            holds += 1
            continue
        now = item['at']
        day = str(now.astimezone(IST).date())
        if open_until is not None and now < open_until:
            vetoes['ONE_POSITION_ACTIVE'] += 1
            continue
        if entries[day] >= scenario['entry_cap']:
            vetoes['DAILY_ENTRY_CAP'] += 1
            continue
        if last_exit is not None and (now - last_exit).total_seconds() < scenario['cooldown_seconds']:
            vetoes['POST_EXIT_COOLDOWN'] += 1
            continue
        if scenario['event_window'] == 'enforce' and 'HIGH_IMPACT_EVENT_WINDOW' in item.get('block_reason_codes', []):
            vetoes['HIGH_IMPACT_EVENT_WINDOW'] += 1
            continue
        entry, reason = _enter({**item, 'tape': bundle.get('market_tape') or []}, cash, realized)
        if entry is None:
            exclusions[reason] += 1
            continue
        walked = walk_exit(entry['position'], bundle.get('market_tape') or [])
        if walked['status'] != 'closed':
            exclusions['exit_path_missing'] += 1
            continue
        proceeds = D(0)
        exit_costs = D(0)
        for fill in walked['fills']:
            price, qty = D(fill['price']), fill['quantity']
            exit_costs += D(fill['costs']['total'])
            proceeds += price * qty - D(fill['costs']['total'])
        pnl = proceeds - entry['cost']
        cash = cash - entry['cost'] + proceeds
        realized += pnl
        open_until = stamp(walked['fills'][-1]['at'])
        last_exit = open_until
        entries[day] += 1
        trades.append({'occurrence_id': item['occurrence_id'], 'net_pnl': pnl, 'costs': entry['entry_costs'] + exit_costs,
                       'playbook_id': item['plan'].get('playbook_id'), 'exited_at': open_until.isoformat()})
    mean, low, high = _interval([trade['net_pnl'] for trade in trades])
    attempted = len(opportunities)
    veto_total = sum(vetoes.values())
    return {
        'policy': policy, 'trades': len(trades), 'policy_holds': holds,
        'net_pnl': _money(sum((trade['net_pnl'] for trade in trades), D(0))),
        'costs': _money(sum((trade['costs'] for trade in trades), D(0))),
        'expectancy': _money(mean), 'uncertainty_low': _money(low), 'uncertainty_high': _money(high),
        'vetoes': dict(sorted(vetoes.items())),
        'veto_rate': _money(D(veto_total) / attempted if attempted else None),
        'exclusions': dict(sorted(exclusions.items())),
        'exit_path_missing': exclusions.get('exit_path_missing', 0),
        'unclassified': 0,
    }


def unbiased_baselines(bundle):
    """One frozen opportunity stream, three policies, pre-registered scenarios."""
    from .options_evaluation import _measurable_opportunities

    opportunities = list(_measurable_opportunities(bundle or {}))
    bought = _ai_buys(bundle or {})
    scenarios = []
    for scenario in BASELINE_SCENARIOS:
        policies = [_portfolio(opportunities, bundle or {}, scenario, policy, bought) for policy in POLICIES]
        scenarios.append({**scenario, 'opportunities': len(opportunities), 'policies': policies})
    production = scenarios[0]
    exclusions = sum(sum(policy['exclusions'].values()) for policy in production['policies'])
    if not opportunities or exclusions:
        status = 'incomplete'
    else:
        status = 'pass'
    return {
        'format': 'options-unbiased-baselines-v1', 'status': status, 'scenarios': scenarios,
        'decision_scenario': 'production', 'scenario_selection': 'pre_registered',
        'promotion_eligible': False, 'rules_changed': False, 'live_orders': 'disabled',
        'future_quotes_used_before_decision': False,
        'limitations': [
            'Scenarios are reported together. None is chosen from held-out returns.',
            'HOLD, AI and the deterministic plan share one opportunity stream.',
            'An entry without a complete v5 exit path is excluded and keeps the report incomplete.',
            'Production entry caps, cooldown and the two-hour event window stay unchanged.',
        ],
    }


def parse_contract_notes(rows):
    """Validate redacted contract-note rows. This function does not write a ledger."""
    parsed = []
    for index, row in enumerate(rows, start=1):
        present = {key for key, value in row.items() if str(value or '').strip()}
        leaked = sorted(present & FORBIDDEN_NOTE_FIELDS)
        if leaked:
            raise ValueError(f'row {index} is not redacted: {", ".join(leaked)}')
        position_id = str(row.get('position_id') or '').strip()
        source = str(row.get('source_reference') or '').strip()
        day = str(row.get('trading_date') or '').strip()
        if not position_id or not source or not day:
            raise ValueError(f'row {index} needs position_id, trading_date and source_reference')
        components = {}
        for field in COST_COMPONENTS:
            if str(row.get(field) or '').strip():
                components[field] = _decimal(row[field])
                if components[field] < 0:
                    raise ValueError(f'row {index} has a negative {field}')
        total = components.get('total', _decimal(row['actual_cost']) if str(row.get('actual_cost') or '').strip() else None)
        if total is None or total < 0:
            raise ValueError(f'row {index} needs actual_cost or total')
        components['total'] = total
        parsed.append({
            'position_id': position_id, 'trading_date': day, 'source_reference': source,
            'actual_cost': str(total),
            'match_key': f'{position_id}|{source}|{BROKER_COST_VERSION}',
            'components': {key: str(value) for key, value in components.items()},
            'provenance': {
                'source_reference': source,
                'schedule': str(row.get('schedule') or BROKER_COST_VERSION),
                'fill_price': str(row['fill_price']) if str(row.get('fill_price') or '').strip() else None,
                'quantity': str(row['quantity']) if str(row.get('quantity') or '').strip() else None,
                'exchange_time': str(row['exchange_time']) if str(row.get('exchange_time') or '').strip() else None,
            },
        })
    return parsed


def _estimated_costs(bundle):
    estimated, meta = {}, {}
    for event in bundle.get('events') or []:
        detail = event.get('detail') or {}
        if event.get('kind') == 'paper_entry':
            position_id = str((detail.get('position') or {}).get('id') or '')
            costs = detail.get('entry_costs') or {}
            meta.setdefault(position_id, {'day': str(event.get('trading_date') or ''), 'price': (detail.get('position') or {}).get('entry'),
                                          'quantity': (detail.get('position') or {}).get('quantity'),
                                          'stamp': (detail.get('quote') or {}).get('stamp')})
        elif event.get('kind') == 'paper_exit':
            position_id = str(detail.get('position_id') or '')
            costs = detail.get('exit_costs') or {}
            current = meta.setdefault(position_id, {'day': str(event.get('trading_date') or '')})
            current.setdefault('exit_price', detail.get('price'))
            current.setdefault('exit_quantity', detail.get('quantity'))
            current.setdefault('exit_stamp', (detail.get('quote') or {}).get('stamp'))
        else:
            continue
        if not position_id or costs.get('total') is None:
            continue
        bucket = estimated.setdefault(position_id, {field: D(0) for field in COST_COMPONENTS})
        for field in COST_COMPONENTS:
            if costs.get(field) is not None:
                bucket[field] += _decimal(costs[field])
    return estimated, meta


def _explain(difference, estimate_day, note):
    reasons = []
    if abs(difference) <= PAISA:
        reasons.append('within_paisa')
    elif abs(difference.quantize(PAISA) - difference) == 0 and abs(difference) <= D('0.05'):
        reasons.append('rounding')
    if estimate_day and note['trading_date'] != estimate_day:
        reasons.append('trading_date')
    if note['provenance']['schedule'] != BROKER_COST_VERSION:
        reasons.append('schedule')
    return reasons or ['unclassified']


def fee_reconciliation(bundle, notes=None):
    """Compare versioned paper fees with redacted notes. The ledger is not rewritten."""
    supplied = notes if notes is not None else (bundle or {}).get('broker_cost_evidence') or []
    if supplied and isinstance(supplied[0], dict) and 'match_key' not in supplied[0]:
        normalized = []
        for row in supplied:
            detail = row.get('detail') or {}
            normalized.append({
                'position_id': row.get('position_id'), 'trading_date': str(row.get('trading_date') or detail.get('trading_date') or ''),
                'source_reference': row.get('source_reference') or detail.get('source_reference') or '',
                'actual_cost': row.get('actual_cost'),
                'components': detail.get('components') or ({'total': str(row.get('actual_cost'))} if row.get('actual_cost') is not None else {}),
                'provenance': detail.get('provenance') or {'source_reference': row.get('source_reference'), 'schedule': BROKER_COST_VERSION,
                                                           'fill_price': detail.get('fill_price'), 'quantity': detail.get('quantity'),
                                                           'exchange_time': detail.get('exchange_time')},
                'match_key': f"{row.get('position_id')}|{row.get('source_reference')}|{BROKER_COST_VERSION}",
            })
        supplied = normalized
    estimates, meta = _estimated_costs(bundle or {})
    notes_by_position = {}
    for note in supplied:
        notes_by_position.setdefault(str(note.get('position_id')), []).append(note)
    rows = []
    for position_id in sorted(set(estimates) | set(notes_by_position)):
        estimate = estimates.get(position_id)
        note_rows = notes_by_position.get(position_id) or []
        note = note_rows[0] if note_rows else None
        components = (note or {}).get('components') or {}
        actual_total = _decimal(note['actual_cost']) if note and note.get('actual_cost') is not None else None
        estimate_total = estimate['total'] if estimate else None
        difference = estimate_total - actual_total if estimate_total is not None and actual_total is not None else None
        explanations = _explain(difference, (meta.get(position_id) or {}).get('day'), note) if difference is not None and note else []
        if estimate is not None and components:
            component_gaps = [
                field for field in COST_COMPONENTS
                if field in components and abs(estimate[field] - _decimal(components[field])) > PAISA
            ]
            if component_gaps and explanations == ['within_paisa']:
                explanations = ['component_mismatch:' + ','.join(component_gaps)]
        stress = {'slippage': None, 'latency_seconds': None, 'partial_fill': None}
        info = meta.get(position_id) or {}
        provenance = (note or {}).get('provenance') or {}
        if provenance.get('fill_price') and info.get('exit_price'):
            stress['slippage'] = str(_decimal(info['exit_price']) - _decimal(provenance['fill_price']))
        if provenance.get('exchange_time') and info.get('exit_stamp'):
            stress['latency_seconds'] = str((stamp(info['exit_stamp']) - stamp(provenance['exchange_time'])).total_seconds())
        if provenance.get('quantity') and info.get('exit_quantity') is not None:
            stress['partial_fill'] = int(info['exit_quantity']) != int(provenance['quantity'])
        missing = [field for field in COST_COMPONENTS if field not in components]
        if note is None or estimate is None:
            row_status = 'missing_evidence'
        elif missing:
            row_status = 'components_absent'
        elif 'unclassified' in explanations or any(item.startswith('component_mismatch') for item in explanations):
            row_status = 'mismatch'
        else:
            row_status = 'reconciled'
        rows.append({
            'position_id': position_id, 'match_key': (note or {}).get('match_key'),
            'estimated_total': _money(estimate_total), 'actual_total': _money(actual_total),
            'difference': _money(difference), 'explanations': explanations, 'stress': stress,
            'components_absent': missing, 'status': row_status,
        })
    if not rows or any(row['status'] in {'missing_evidence', 'components_absent'} for row in rows):
        status = 'incomplete'
    elif any(row['status'] == 'mismatch' for row in rows):
        status = 'fail'
    else:
        status = 'pass'
    return {
        'format': 'options-cost-reconciliation-v1', 'version': BROKER_COST_VERSION, 'status': status,
        'positions': rows, 'ledger_rewritten': False, 'live_orders': 'disabled',
        'limitations': [
            'Notes without component fields stay incomplete.',
            'Slippage, latency and partial-fill stress appear only where both sides record the input.',
            'Historical paper events are left unchanged.',
        ],
    }


def _release_calls(bundle, day, release):
    return [call for call in bundle.get('calls') or []
            if str(call.get('trading_date')) == day
            and (call.get('snapshot') or {}).get('provenance', {}).get('release') == release]


def release_acceptance(bundle, day, release):
    """Release-bound paper chain. A quiet session stays incomplete."""
    calls = _release_calls(bundle, day, release)
    events = [event for event in bundle.get('events') or [] if str(event.get('trading_date')) == day]
    observations = [row for row in bundle.get('market_tape') or [] if str(row.get('trading_date')) == day
                    and (row.get('market_snapshot') or {}).get('provenance', {}).get('release') == release]
    plans = []
    for call in calls:
        plans.extend(((call.get('snapshot') or {}).get('strategy_selection') or {}).get('plans') or [])
    for event in events:
        if event.get('kind') == 'strategy_scan':
            plans.extend((event.get('detail') or {}).get('plans') or [])
    decided = [call for call in calls if call.get('state') in {'applied', 'rejected'} and (call.get('result') or {}).get('decision')]
    ids = {str(call.get('call_id')) for call in decided}
    entries = [event for event in events if event.get('kind') == 'paper_entry' and str((event.get('detail') or {}).get('call_id')) in ids]
    positions = {str((event.get('detail') or {}).get('position', {}).get('id')) for event in entries}
    exits = [event for event in events if event.get('kind') == 'paper_exit'
             and str((event.get('detail') or {}).get('position_id')) in positions and (event.get('detail') or {}).get('closed')]
    independent = [event for event in exits if (event.get('detail') or {}).get('reason') in DETERMINISTIC_REASONS]
    reports = [row for row in bundle.get('reports') or [] if str(row.get('trading_date')) == day]
    flat = [row for row in reports if (row.get('report') or {}).get('reconciled_flat')]
    delivered = [row for row in flat if row.get('delivery_status') == 'sent']
    replayed = replay({'calls': calls, 'events': events})
    call_rows = [row for row in replayed['calls'] if row['status'] != 'legacy_unreplayable']
    fill_rows = replayed['fills']
    parity = bool(entries) and bool(call_rows) and all(row['status'] == 'match' for row in call_rows + fill_rows)
    checks = {
        'observation': bool(observations),
        'plan': bool(plans),
        'decision': bool(decided),
        'accepted_fill': bool(entries),
        'independent_exit': bool(entries) and len(independent) == len(exits) and {event['detail']['position_id'] for event in independent} >= positions,
        'replay_parity': parity,
        'reconciliation': bool(flat) and parity,
        'report_delivery': bool(delivered),
    }
    no_setup = bool(observations) and not entries
    status = 'complete' if all(checks.values()) else 'incomplete'
    evidence = {'day': day, 'release': release, 'calls': len(calls), 'events': len(events), 'observations': len(observations)}
    return {
        'format': 'options-release-acceptance-v1', 'day': day, 'release': release, 'status': status,
        'checks': checks, 'missing': [key for key, ok in checks.items() if not ok],
        'no_setup': no_setup, 'checksum_sha256': _sha(evidence | {'checks': checks}),
        'replay': {'matching': replayed['matching'], 'mismatches': replayed['mismatches'], 'fills': fill_rows},
        'forced_entry': False, 'live_orders': 'disabled',
        'note': 'A session without a paper entry stays incomplete. This check does not create one.',
    }
