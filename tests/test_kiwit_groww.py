import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kiwit.brokers.groww import BrokerApiError, BrokerExecutionDisabled, GrowwBrokerClient, GrowwSettings


class FakeTransport:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status = status
        self.requests = []

    def __call__(self, request, timeout):
        self.requests.append((request, timeout))
        return self.status, json.dumps(self.payload).encode()


class GrowwBrokerTests(unittest.TestCase):
    def settings(self):
        return GrowwSettings(access_token="token-that-is-long-enough-for-test")

    def test_profile_uses_official_origin_and_required_headers(self):
        transport = FakeTransport({"status": "SUCCESS", "payload": {"nse_enabled": True}})
        client = GrowwBrokerClient(self.settings(), transport)
        self.assertTrue(client.profile()["nse_enabled"])
        request, timeout = transport.requests[0]
        self.assertEqual(request.full_url, "https://api.groww.in/v1/user/detail")
        self.assertEqual(request.get_header("X-api-version"), "1.0")
        self.assertTrue(request.get_header("Authorization").startswith("Bearer "))
        self.assertEqual(timeout, 8)

    def test_holdings_and_positions_parse_payload(self):
        holdings = FakeTransport({"status": "SUCCESS", "payload": {"holdings": [{"trading_symbol": "NIFTYBEES"}]}})
        self.assertEqual(GrowwBrokerClient(self.settings(), holdings).holdings()[0]["trading_symbol"], "NIFTYBEES")
        positions = FakeTransport({"status": "SUCCESS", "payload": {"positions": [{"quantity": 2}]}})
        client = GrowwBrokerClient(self.settings(), positions)
        self.assertEqual(client.positions()[0]["quantity"], 2)
        self.assertIn("segment=CASH", positions.requests[0][0].full_url)

    def test_failure_is_sanitized_and_token_is_not_exposed(self):
        token = self.settings().access_token
        transport = FakeTransport({"status": "FAILURE", "error": {"code": "GA001", "message": token}})
        with self.assertRaises(BrokerApiError) as caught:
            GrowwBrokerClient(self.settings(), transport).profile()
        self.assertNotIn(token, str(caught.exception))
        self.assertIn("GA001", str(caught.exception))

    def test_mutating_orders_are_hard_disabled(self):
        transport = FakeTransport({"status": "SUCCESS", "payload": {}})
        client = GrowwBrokerClient(self.settings(), transport)
        with self.assertRaises(BrokerExecutionDisabled):
            client.place_order({"trading_symbol": "NIFTYBEES"})
        with self.assertRaises(BrokerExecutionDisabled):
            client.cancel_order("order-123")
        self.assertEqual(transport.requests, [])

    def test_environment_requires_a_real_token_length(self):
        with (
            patch.dict(os.environ, {"KIWIT_GROWW_ACCESS_TOKEN": "short"}, clear=False),
            self.assertRaises(ValueError),
        ):
            GrowwSettings.from_env()

    def test_rejects_non_official_origin_and_invalid_identifiers(self):
        with self.assertRaises(ValueError):
            GrowwSettings(access_token="token-that-is-long-enough-for-test", base_url="https://evil.invalid")
        client = GrowwBrokerClient(self.settings(), FakeTransport({"status": "SUCCESS", "payload": {}}))
        with self.assertRaises(ValueError):
            client.order_status("../secrets")
        with self.assertRaises(ValueError):
            client.quote("bad symbol!")


if __name__ == "__main__":
    unittest.main()
