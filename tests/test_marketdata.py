import csv
import io
import sys
import tempfile
import unittest
import zipfile
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kiwit.marketdata.models import NormalizedBar
from kiwit.marketdata.parsers import parse_udiff_equity
from kiwit.marketdata.reference import InstrumentRecord, MembershipInterval, ObservedTradingCalendar, validate_instruments, validate_membership
from kiwit.marketdata.validation import validate_alignment, validate_bars, validate_freshness


class MarketDataTests(unittest.TestCase):
    def test_udiff_parser_uses_named_columns(self):
        headers = ["Rsvd04", "TradDt", "ClsPric", "TckrSymb", "SctySrs", "OpnPric", "HghPric", "LwPric", "TtlTradgVol", "ISIN"]
        row = {"Rsvd04": "", "TradDt": "2024-07-08", "ClsPric": "250", "TckrSymb": "NIFTYBEES", "SctySrs": "EQ", "OpnPric": "249", "HghPric": "251", "LwPric": "248", "TtlTradgVol": "1000", "ISIN": "INF204KB14I2"}
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "sample.zip"
            output = io.StringIO()
            writer = csv.DictWriter(output, fieldnames=headers)
            writer.writeheader(); writer.writerow(row)
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("sample.csv", output.getvalue())
            bar = parse_udiff_equity(path, "NIFTYBEES")
            self.assertEqual(bar.close, 250)
            self.assertEqual(bar.isin, "INF204KB14I2")

    def test_invalid_ohlc_is_quarantined(self):
        bar = NormalizedBar(date(2024, 1, 1), "X", "EQ", 100, 99, 98, 100, 1, "a" * 64, "test")
        report = validate_bars("x", [bar])
        self.assertFalse(report.publishable)

    def test_alignment_missing_date_is_error(self):
        bar = NormalizedBar(date(2024, 1, 1), "X", "EQ", 100, 101, 99, 100, 1, "a" * 64, "test")
        report = validate_alignment("x", [bar], "index", [])
        self.assertFalse(report.publishable)

    def test_membership_overlap_is_rejected(self):
        intervals = [
            MembershipInterval("NIFTY100", "X", date(2024, 1, 1), date(2024, 6, 30), "source"),
            MembershipInterval("NIFTY100", "X", date(2024, 6, 30), None, "source"),
        ]
        with self.assertRaises(ValueError):
            validate_membership(intervals)

    def test_stale_dataset_fails_closed(self):
        bar = NormalizedBar(date(2024, 1, 1), "X", "EQ", 100, 101, 99, 100, 1, "a" * 64, "test")
        self.assertFalse(validate_freshness("x", [bar], date(2024, 1, 10)).publishable)

    def test_observed_calendar_and_instrument_validation(self):
        calendar = ObservedTradingCalendar(frozenset({date(2024, 1, 1)}))
        self.assertTrue(calendar.is_session(date(2024, 1, 1)))
        validate_instruments([InstrumentRecord("NSE", "X", "EQ", "ISIN", date(2024, 1, 1))])
