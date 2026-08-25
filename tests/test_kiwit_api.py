import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fastapi.testclient import TestClient

from kiwit.api import create_app


class FakeLedger:
    def __init__(self):
        self.halted = False

    def account_status(self, account_id):
        if account_id == "missing":
            raise KeyError(account_id)
        return {"account_id": account_id, "cash_balance": "1000", "realized_pnl": "0", "status": "active", "execution_halted": self.halted, "positions": []}

    def halt(self, account_id, reason_code, reason):
        self.halted = True

    def release_halt(self, account_id, operator):
        self.halted = False


class FakeKnowledge:
    def search(self, query, limit=6):
        return [SimpleNamespace(chunk_id="abc", citation="[abc] Risk Book, p. 7", content="Risk first.", score=1.0, source_type="book")]


class FakeBroker:
    def profile(self):
        return {"nse_enabled": True, "active_segments": ["CASH"]}

    def holdings(self):
        return [{"trading_symbol": "NIFTYBEES", "quantity": 1}]

    def positions(self, segment="CASH"):
        return [{"trading_symbol": "NIFTYBEES", "segment": segment}]


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.key = "a-secure-test-key-that-is-long-enough"
        self.environment = patch.dict(os.environ, {"KIWIT_API_KEY": self.key}, clear=False)
        self.environment.start()
        self.client = TestClient(create_app(ledger=FakeLedger(), knowledge_index=FakeKnowledge(), broker_client=FakeBroker()))
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.environment.stop()

    def test_health_is_public_but_account_is_protected(self):
        self.assertEqual(self.client.get("/health").status_code, 200)
        self.assertEqual(self.client.get("/health").json()["release"], "development")
        self.assertEqual(self.client.get("/live").status_code, 200)
        self.assertEqual(self.client.get("/ready").json()["database"], "injected")
        self.assertEqual(self.client.get("/api/v1/paper/accounts/test").status_code, 401)

    def test_metrics_are_authenticated_and_record_requests(self):
        self.client.get("/health")
        self.assertEqual(self.client.get("/metrics").status_code, 401)
        response = self.client.get("/metrics", headers={"X-KIWIT-API-Key": self.key})
        self.assertEqual(response.status_code, 200)
        self.assertIn("kiwit_http_requests_total", response.text)
        self.assertIn('route="/health"', response.text)

    def test_request_id_is_returned(self):
        response = self.client.get("/health", headers={"X-Request-ID": "operator-123"})
        self.assertEqual(response.headers["X-Request-ID"], "operator-123")

    def test_untrusted_host_and_large_body_are_rejected(self):
        self.assertEqual(self.client.get("/health", headers={"Host": "attacker.invalid"}).status_code, 400)
        response = self.client.post(
            "/api/v1/research/search",
            headers={"X-KIWIT-API-Key": self.key, "Content-Length": "65537"},
            json={"query": "risk"},
        )
        self.assertEqual(response.status_code, 413)

    def test_previous_api_key_supports_safe_rotation(self):
        previous = "a-previous-test-key-that-is-long-enough"
        with patch.dict(os.environ, {"KIWIT_PREVIOUS_API_KEY": previous}, clear=False):
            response = self.client.get("/api/v1/paper/accounts/test", headers={"X-KIWIT-API-Key": previous})
        self.assertEqual(response.status_code, 200)

    def test_broker_endpoints_are_read_only_and_protected(self):
        headers = {"X-KIWIT-API-Key": self.key}
        self.assertEqual(self.client.get("/api/v1/broker/status").status_code, 401)
        status_response = self.client.get("/api/v1/broker/status", headers=headers).json()
        self.assertTrue(status_response["configured"])
        self.assertEqual(status_response["execution"], "disabled")
        self.assertTrue(self.client.get("/api/v1/broker/profile", headers=headers).json()["profile"]["nse_enabled"])
        self.assertEqual(len(self.client.get("/api/v1/broker/holdings", headers=headers).json()["holdings"]), 1)

    def test_account_and_kill_switch(self):
        headers = {"X-KIWIT-API-Key": self.key}
        self.assertFalse(self.client.get("/api/v1/paper/accounts/test", headers=headers).json()["execution_halted"])
        halted = self.client.post("/api/v1/paper/accounts/test/halt", headers=headers, json={"reason": "safety test"})
        self.assertTrue(halted.json()["execution_halted"])

    def test_rag_search_returns_citation(self):
        response = self.client.post(
            "/api/v1/research/search", headers={"X-KIWIT-API-Key": self.key}, json={"query": "risk sizing"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("p. 7", response.json()["hits"][0]["citation"])

    def test_security_headers_are_set(self):
        response = self.client.get("/dashboard")
        self.assertEqual(response.headers["x-frame-options"], "DENY")
        self.assertIn("default-src 'self'", response.headers["content-security-policy"])
