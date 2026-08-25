import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kiwit.baselines import donchian_baseline
from kiwit.research import bootstrap_trades, passive_benchmark, randomized_entry_control


class StrategyResearchTests(unittest.TestCase):
    def data(self):
        dates = pd.bdate_range("2018-01-01", periods=800)
        close = 100 + np.arange(len(dates)) * 0.1 + np.sin(np.arange(len(dates)) / 10) * 4
        return pd.DataFrame({"open": close, "high": close + 1, "low": close - 1, "close": close, "volume": 1000}, index=dates)

    def test_random_control_is_seed_reproducible(self):
        params = {"entry_window": 50, "exit_window": 20}
        first = randomized_entry_control(self.data(), "donchian", params, simulations=20, seed=7)
        second = randomized_entry_control(self.data(), "donchian", params, simulations=20, seed=7)
        self.assertEqual(first, second)

    def test_bootstrap_is_seed_reproducible(self):
        trades = donchian_baseline(self.data()).trades
        self.assertEqual(bootstrap_trades(trades, 100, 7), bootstrap_trades(trades, 100, 7))

    def test_passive_benchmark_enters_once(self):
        result = passive_benchmark(self.data())
        self.assertEqual(result.metrics["trade_count"], 1)

