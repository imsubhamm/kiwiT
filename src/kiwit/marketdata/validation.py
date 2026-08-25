from __future__ import annotations

from datetime import date
from statistics import median

from .models import NormalizedBar, Severity, ValidationIssue, ValidationReport


def validate_bars(name: str, bars: list[NormalizedBar], known_action_dates: set[date] | None = None) -> ValidationReport:
    actions = known_action_dates or set()
    issues: list[ValidationIssue] = []
    ordered = sorted(bars, key=lambda bar: bar.trading_date)
    dates = [bar.trading_date for bar in ordered]
    if len(dates) != len(set(dates)):
        issues.append(ValidationIssue(Severity.ERROR, "DUPLICATE_DATE", "duplicate trading dates detected"))
    for bar in ordered:
        if min(bar.open, bar.high, bar.low, bar.close) <= 0:
            issues.append(ValidationIssue(Severity.ERROR, "NON_POSITIVE_PRICE", "OHLC contains a non-positive value", bar.trading_date))
        if bar.high < max(bar.open, bar.close, bar.low) or bar.low > min(bar.open, bar.close, bar.high):
            issues.append(ValidationIssue(Severity.ERROR, "INVALID_OHLC", "high/low does not contain open and close", bar.trading_date))
        if bar.volume is not None and bar.volume < 0:
            issues.append(ValidationIssue(Severity.ERROR, "NEGATIVE_VOLUME", "volume is negative", bar.trading_date))
    returns = []
    for previous, current in zip(ordered, ordered[1:]):
        change = current.close / previous.close - 1
        returns.append(abs(change))
        if abs(change) > 0.40 and current.trading_date not in actions:
            issues.append(ValidationIssue(Severity.ERROR, "UNEXPLAINED_PRICE_JUMP", f"close changed {change:.2%}", current.trading_date))
    if returns and median(returns) == 0:
        issues.append(ValidationIssue(Severity.WARNING, "ZERO_MEDIAN_RETURN", "median absolute return is zero"))
    return ValidationReport(name, len(ordered), tuple(issues))


def validate_alignment(left_name: str, left: list[NormalizedBar], right_name: str, right: list[NormalizedBar]) -> ValidationReport:
    left_dates = {bar.trading_date for bar in left}
    right_dates = {bar.trading_date for bar in right}
    issues = []
    for day in sorted(left_dates - right_dates):
        issues.append(ValidationIssue(Severity.ERROR, "MISSING_MATCHED_DATE", f"{right_name} missing date present in {left_name}", day))
    for day in sorted(right_dates - left_dates):
        issues.append(ValidationIssue(Severity.WARNING, "UNMATCHED_DATE", f"{left_name} missing date present in {right_name}", day))
    return ValidationReport(f"{left_name}_vs_{right_name}", len(left_dates & right_dates), tuple(issues))


def validate_freshness(name: str, bars: list[NormalizedBar], as_of: date, maximum_calendar_days: int = 7) -> ValidationReport:
    if not bars:
        return ValidationReport(name, 0, (ValidationIssue(Severity.ERROR, "EMPTY_DATASET", "dataset contains no bars"),))
    latest = max(bar.trading_date for bar in bars)
    age = (as_of - latest).days
    issues = () if age <= maximum_calendar_days else (
        ValidationIssue(Severity.ERROR, "STALE_DATA", f"latest bar is {age} calendar days old", latest),
    )
    return ValidationReport(name, len(bars), issues)
