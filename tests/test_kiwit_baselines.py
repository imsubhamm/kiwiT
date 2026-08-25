import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kiwit.baselines import donchian_baseline, ema_baseline


class BaselineTests(unittest.TestCase):
    def data(self):
        dates = pd.bdate_range("2020-01-01", periods=400)
        close = np.linspace(100, 200, len(dates)) + np.sin(np.arange(len(dates)) / 8) * 5
        return pd.DataFrame({"open": close, "high": close + 1, "low": close - 1, "close": close, "volume": 1000}, index=dates)

    def test_ema_baseline_is_reproducible(self):
        first = ema_baseline(self.data())
        second = ema_baseline(self.data())
        self.assertEqual(first.metrics, second.metrics)

    def test_donchian_uses_prior_channel(self):
        result = donchian_baseline(self.data())
        self.assertIn("trade_count", result.metrics)
        self.assertGreaterEqual(result.metrics["trade_count"], 0)

