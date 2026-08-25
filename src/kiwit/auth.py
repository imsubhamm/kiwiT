from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .database import PostgresDatabase

SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1


def hash_password(password: str, salt: bytes | None = None) -> str:
    if len(password) < 16:
        raise ValueError("password must contain at least 16 characters")
    salt = salt or secrets.token_bytes(16)
    derived = hashlib.scrypt(password.encode(), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=32)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${base64.urlsafe_b64encode(salt).decode()}${base64.urlsafe_b64encode(derived).decode()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, n, r, p, salt, expected = encoded.split("$", 5)
        if algorithm != "scrypt":
            return False
        derived = hashlib.scrypt(
            password.encode(), salt=base64.urlsafe_b64decode(salt), n=int(n), r=int(r), p=int(p), dklen=32
        )
        return hmac.compare_digest(base64.urlsafe_b64encode(derived).decode(), expected)
    except (ValueError, TypeError):
        return False


@dataclass(frozen=True)
class AuthenticatedUser:
    user_id: str
    email: str
    role: str


class PostgresSessionAuth:
    def __init__(self, database: PostgresDatabase, session_hours: int = 12) -> None:
        self.database = database
        self.session_hours = session_hours

    def bootstrap_admin(self) -> None:
        email = os.getenv("KIWIT_ADMIN_EMAIL", "").strip().lower()
        password_hash = os.getenv("KIWIT_ADMIN_PASSWORD_HASH", "")
        if not email or not password_hash.startswith("scrypt$"):
            return
        with self.database.transaction() as connection:
            connection.execute(
                "INSERT INTO app_users(user_id,email,password_hash,role,active) VALUES(%s,%s,%s,'super_admin',true) "
                "ON CONFLICT(email) DO UPDATE SET password_hash=EXCLUDED.password_hash,active=true",
                (str(secrets.token_hex(16)), email, password_hash),
            )

    def login(self, email: str, password: str, remote_address: str) -> tuple[str, AuthenticatedUser] | None:
        normalized = email.strip().lower()
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT user_id,email,password_hash,role,active FROM app_users WHERE email=%s FOR UPDATE", (normalized,)
            ).fetchone()
            if not row or not row[4] or not verify_password(password, row[2]):
                return None
            token = secrets.token_urlsafe(48)
            token_hash = hashlib.sha256(token.encode()).hexdigest()
            now = datetime.now(UTC)
            connection.execute(
                "INSERT INTO app_sessions(session_id,user_id,token_sha256,created_at,expires_at,remote_address) "
                "VALUES(%s,%s,%s,%s,%s,%s)",
                (str(secrets.token_hex(16)), row[0], token_hash, now, now + timedelta(hours=self.session_hours),
                 remote_address[:128]),
            )
        return token, AuthenticatedUser(str(row[0]), row[1], row[3])

    def authenticate(self, token: str) -> AuthenticatedUser | None:
        if len(token) < 32:
            return None
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        with self.database.connect(autocommit=True) as connection:
            row = connection.execute(
                "SELECT u.user_id,u.email,u.role FROM app_sessions s JOIN app_users u USING(user_id) "
                "WHERE s.token_sha256=%s AND s.revoked_at IS NULL AND s.expires_at>now() AND u.active",
                (token_hash,),
            ).fetchone()
        return AuthenticatedUser(str(row[0]), row[1], row[2]) if row else None

    def logout(self, token: str) -> None:
        if not token:
            return
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE app_sessions SET revoked_at=now() WHERE token_sha256=%s AND revoked_at IS NULL", (token_hash,)
            )


class MemorySessionAuth:
    """Test/local adapter with the same interface; production uses PostgreSQL."""

    def __init__(self, email: str, password: str) -> None:
        self.email = email.lower()
        self.password_hash = hash_password(password)
        self.sessions: dict[str, AuthenticatedUser] = {}

    def bootstrap_admin(self) -> None:
        return None

    def login(self, email: str, password: str, remote_address: str) -> tuple[str, AuthenticatedUser] | None:
        del remote_address
        if email.lower() != self.email or not verify_password(password, self.password_hash):
            return None
        token = secrets.token_urlsafe(48)
        user = AuthenticatedUser("test-user", self.email, "super_admin")
        self.sessions[token] = user
        return token, user

    def authenticate(self, token: str) -> AuthenticatedUser | None:
        return self.sessions.get(token)

    def logout(self, token: str) -> None:
        self.sessions.pop(token, None)
