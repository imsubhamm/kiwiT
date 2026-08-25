import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tradingkiwi.backtest import prepare, run_backtest
from tradingkiwi.nse_data import adjust_splits


class BacktestTests(unittest.TestCase):
    def test_split_adjustment_preserves_notional(self):
        frame = pd.DataFrame({"date": ["2019-12-18", "2019-12-19"], "open": [1000.0, 101.0], "high": [1010.0, 102.0], "low": [990.0, 100.0], "close": [1000.0, 101.0], "volume": [100, 1000]})
        actions = pd.DataFrame({"ex_date": ["2019-12-19"], "action_type": ["split"], "ratio": [10]})
        adjusted = adjust_splits(frame, actions)
        self.assertEqual(adjusted.loc[0, "close"], 100.0)
        self.assertEqual(adjusted.loc[0, "volume"], 1000)

    def test_prepare_has_no_future_signal_dependency(self):
        dates = pd.bdate_range("2020-01-01", periods=320)
        close = np.linspace(100, 180, len(dates)) + np.sin(np.arange(len(dates)) / 5) * 3
        equity = pd.DataFrame({"date": dates, "open": close, "high": close + 1, "low": close - 1, "close": close, "volume": 1_000_000})
        index = pd.DataFrame({"date": dates, "open": close * 100, "high": close * 101, "low": close * 99, "close": close * 100})
        before = prepare(equity.iloc[:-1], index.iloc[:-1])
        after = prepare(equity, index)
        common = before.index.intersection(after.index)
        pd.testing.assert_series_equal(before.loc[common, "signal"], after.loc[common, "signal"])

    def test_empty_signal_run_preserves_capital(self):
        dates = pd.bdate_range("2020-01-01", periods=50)
        frame = pd.DataFrame({"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "atr14": 2.0, "signal": False}, index=dates)
        trades, curve, summary = run_backtest(frame)
        self.assertTrue(trades.empty)
        self.assertEqual(summary["final_equity"], 1_000_000)
        self.assertTrue((curve["equity"] == 1_000_000).all())


if __name__ == "__main__":
    unittest.main()
