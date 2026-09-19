"""Versioned NSE F&O regular-session calendar; no inference from missing candles.

Source: https://nsearchives.nseindia.com/content/circulars/FAOP71777.pdf
Amendment: https://nsearchives.nseindia.com/content/circulars/FAOP72262.pdf
Special sessions require an explicit reviewed calendar update; never trade them
using ordinary-session assumptions. The annual circular is not a live feed.
"""
from datetime import date

VERSION = "nse-fno-2026-faop71777-72262-v1"
SOURCE = "https://nsearchives.nseindia.com/content/circulars/FAOP71777.pdf"
HOLIDAYS = frozenset(date.fromisoformat(day) for day in (
    "2026-01-15", "2026-01-26", "2026-03-03", "2026-03-26", "2026-03-31", "2026-04-03",
    "2026-04-14", "2026-05-01", "2026-05-28", "2026-06-26", "2026-09-14",
    "2026-10-02", "2026-10-20", "2026-11-10", "2026-11-24", "2026-12-25",
))


def regular_session(day):
    """None means unverified, False a closure, True an expected regular session."""
    if day.year != 2026:
        return None
    return day.weekday() < 5 and day not in HOLIDAYS
