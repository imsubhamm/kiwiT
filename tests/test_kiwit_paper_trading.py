import sys
import unittest
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kiwit.domain import Instrument, Quote, Side
from kiwit.paper_trading import PaperCostModel


class PaperTradingTests(unittest.TestCase):
    def setUp(self):
        self.instrument = Instrument("NIFTYBEES")
        self.quote = Quote(self.instrument, datetime.now(UTC), Decimal("249.95"), Decimal("250.05"), Decimal(250))

    def test_buy_fill_uses_ask_plus_slippage(self):
        model = PaperCostModel(slippage_bps=Decimal(5), brokerage_bps=Decimal(2), taxes_bps=Decimal(3))
        self.assertEqual(model.fill_price(self.quote, Side.BUY), Decimal("250.175025"))
        self.assertEqual(model.charges(Decimal(10000)), (Decimal(2), Decimal(3)))

    def test_sell_fill_uses_bid_minus_slippage(self):
        model = PaperCostModel(slippage_bps=Decimal(5))
        self.assertLess(model.fill_price(self.quote, Side.SELL), self.quote.bid)

    def test_negative_cost_assumption_is_rejected(self):
        with self.assertRaises(ValueError):
            PaperCostModel(slippage_bps=Decimal(-1))
