import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kiwit.auth import MemorySessionAuth, hash_password, verify_password


class AuthenticationTests(unittest.TestCase):
    def test_password_hash_is_salted_and_verifiable(self):
        password = "correct-horse-battery-staple"
        first = hash_password(password)
        second = hash_password(password)
        self.assertNotEqual(first, second)
        self.assertTrue(verify_password(password, first))
        self.assertFalse(verify_password("incorrect-password-value", first))

    def test_short_password_is_rejected(self):
        with self.assertRaises(ValueError):
            hash_password("admin123")

    def test_sessions_are_revocable(self):
        auth = MemorySessionAuth("super.admin@kiwit.com", "a-strong-test-password-value")
        token, user = auth.login("SUPER.ADMIN@KIWIT.COM", "a-strong-test-password-value", "127.0.0.1")
        self.assertEqual(user.role, "super_admin")
        self.assertIsNotNone(auth.authenticate(token))
        auth.logout(token)
        self.assertIsNone(auth.authenticate(token))


if __name__ == "__main__":
    unittest.main()
