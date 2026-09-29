"""Shared evaluation windows and auditable quote-tracking capacity."""
from datetime import timedelta

ENTRY_DELAY_SECONDS = 90
MAX_HORIZON_MINUTES = 60
QUOTE_GRACE_SECONDS = 90
TRACKED_SYMBOL_CAPACITY = 64
TRACKING_CLASSES = ('ordinary_candidate', 'eligible_plan', 'selected_plan', 'rejected_plan',
                    'opened_position', 'calendar_blocked_shadow')


def retention_window():
    return timedelta(seconds=ENTRY_DELAY_SECONDS + QUOTE_GRACE_SECONDS,
                     minutes=MAX_HORIZON_MINUTES)


def tracking_capacity(rows, *, capacity=TRACKED_SYMBOL_CAPACITY):
    """Prioritize opened positions, then oldest obligations; symbols break ties.

    Rows are (symbol, reasons, first_seen_at). No overflow obligation is deleted.
    """
    ordered = sorted(rows, key=lambda row: ('opened_position' not in row[1], row[2], row[0]))
    selected = [row[0] for row in ordered[:capacity]]
    excluded = [row[0] for row in ordered[capacity:]]
    return {
        'capacity': capacity,
        'policy': 'opened_position_then_first_seen_then_symbol',
        'required_count': len(ordered),
        'selected_symbols': selected,
        'overflow_count': len(excluded),
        'overflow_symbols': excluded,
        'classes': {reason: {'required_symbols': [row[0] for row in ordered if reason in row[1]]}
                    for reason in TRACKING_CLASSES},
    }


def tracking_coverage(tracking, candidates):
    """Count only quotes actually returned; retain symbol-level missing evidence."""
    observed = {candidate['symbol'] for candidate in candidates if candidate.get('quote')}
    overflow = set(tracking['overflow_symbols'])
    return {**tracking, 'classes': {
        reason: {
            **group,
            'required_count': len(group['required_symbols']),
            'observed_count': len(set(group['required_symbols']) & observed),
            'missing_symbols': sorted(set(group['required_symbols']) - observed),
            'overflow_symbols': sorted(set(group['required_symbols']) & overflow),
        } for reason, group in tracking['classes'].items()
    }}
