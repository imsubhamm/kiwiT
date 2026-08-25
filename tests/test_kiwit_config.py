import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kiwit.config import load_config
from kiwit.domain import Environment


class ConfigTests(unittest.TestCase):
    def test_default_configuration_disables_live_execution(self):
        config = load_config("config/kiwit.toml")
        self.assertEqual(config.environment, Environment.RESEARCH)
        self.assertEqual(config.execution.mode, Environment.PAPER)
        self.assertFalse(config.live_execution_enabled)

