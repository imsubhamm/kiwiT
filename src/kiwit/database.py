from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

MIGRATION_PATTERN = re.compile(r"^(\d{3,})_([a-z0-9_]+)\.sql$")
ADVISORY_LOCK_ID = 750_491_084


class DatabaseConfigurationError(ValueError):
    pass


class MigrationError(RuntimeError):
    pass


@dataclass(frozen=True)
class DatabaseSettings:
    url: str = field(repr=False)
    connect_timeout_seconds: int = 10
    application_name: str = "kiwit"

    @classmethod
    def from_env(cls) -> DatabaseSettings:
        url = os.getenv("KIWIT_DATABASE_URL", "")
        if not url:
            raise DatabaseConfigurationError("KIWIT_DATABASE_URL is required")
        if not url.startswith(("postgresql://", "postgres://")):
            raise DatabaseConfigurationError("production database must use PostgreSQL")
        timeout = int(os.getenv("KIWIT_DB_CONNECT_TIMEOUT", "10"))
        if timeout < 1:
            raise DatabaseConfigurationError("KIWIT_DB_CONNECT_TIMEOUT must be positive")
        return cls(url=url, connect_timeout_seconds=timeout)


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    path: Path
    checksum: str


def discover_migrations(directory: str | Path) -> list[Migration]:
    directory = Path(directory)
    migrations: list[Migration] = []
    seen: set[int] = set()
    for path in sorted(directory.glob("*.sql")):
        match = MIGRATION_PATTERN.match(path.name)
        if not match:
            raise MigrationError(f"invalid migration filename: {path.name}")
        version = int(match.group(1))
        if version in seen:
            raise MigrationError(f"duplicate migration version: {version}")
        seen.add(version)
        migrations.append(Migration(version, match.group(2), path, hashlib.sha256(path.read_bytes()).hexdigest()))
    if not migrations:
        raise MigrationError(f"no migrations found in {directory}")
    return migrations


def _psycopg() -> Any:
    try:
        import psycopg
    except ImportError as error:
        raise RuntimeError("install kiwiT with the 'production' extra to use PostgreSQL") from error
    return psycopg


class PostgresDatabase:
    """Small PostgreSQL boundary for migrations, health checks, and transactions."""

    def __init__(self, settings: DatabaseSettings) -> None:
        self.settings = settings

    def connect(self, *, autocommit: bool = False) -> Any:
        return _psycopg().connect(
            self.settings.url,
            autocommit=autocommit,
            connect_timeout=self.settings.connect_timeout_seconds,
            application_name=self.settings.application_name,
        )

    @contextmanager
    def transaction(self) -> Iterator[Any]:
        with self.connect() as connection, connection.transaction():
            yield connection

    def healthcheck(self) -> dict[str, Any]:
        with self.connect(autocommit=True) as connection:
            row = connection.execute(
                "SELECT current_database(), current_user, current_setting('server_version_num')::int"
            ).fetchone()
            migration = connection.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations").fetchone()
        return {"status": "ok", "database": row[0], "user": row[1], "server_version_num": row[2], "schema_version": migration[0]}

    def migrate(self, directory: str | Path) -> list[int]:
        migrations = discover_migrations(directory)
        applied_now: list[int] = []
        with self.connect(autocommit=True) as connection:
            connection.execute("SELECT pg_advisory_lock(%s)", (ADVISORY_LOCK_ID,))
            try:
                table_exists = connection.execute("SELECT to_regclass('public.schema_migrations')").fetchone()[0]
                applied: dict[int, tuple[str | None, str | None]] = {}
                if table_exists:
                    columns = {
                        row[0]
                        for row in connection.execute(
                            "SELECT column_name FROM information_schema.columns WHERE table_schema='public' AND table_name='schema_migrations'"
                        ).fetchall()
                    }
                    if {"name", "checksum"}.issubset(columns):
                        statement = "SELECT version, name, checksum FROM schema_migrations"
                    elif "name" in columns:
                        statement = "SELECT version, name, NULL FROM schema_migrations"
                    elif "checksum" in columns:
                        statement = "SELECT version, NULL, checksum FROM schema_migrations"
                    else:
                        statement = "SELECT version, NULL, NULL FROM schema_migrations"
                    applied = {row[0]: (row[1], row[2]) for row in connection.execute(statement)}

                for migration in migrations:
                    previous = applied.get(migration.version)
                    if previous:
                        if previous[1] and previous[1] != migration.checksum:
                            raise MigrationError(f"checksum mismatch for applied migration {migration.path.name}")
                        continue
                    connection.execute(migration.path.read_text())
                    columns = {
                        row[0]
                        for row in connection.execute(
                            "SELECT column_name FROM information_schema.columns WHERE table_schema='public' AND table_name='schema_migrations'"
                        ).fetchall()
                    }
                    if {"name", "checksum"}.issubset(columns):
                        connection.execute(
                            "UPDATE schema_migrations SET name=%s, checksum=%s WHERE version=%s",
                            (migration.name, migration.checksum, migration.version),
                        )
                    applied_now.append(migration.version)

                # Backfill checksums for migrations created before checksum tracking.
                if {"name", "checksum"}.issubset(columns):
                    for migration in migrations:
                        connection.execute(
                            "UPDATE schema_migrations SET name=COALESCE(name,%s), checksum=COALESCE(checksum,%s) WHERE version=%s",
                            (migration.name, migration.checksum, migration.version),
                        )
            finally:
                connection.execute("SELECT pg_advisory_unlock(%s)", (ADVISORY_LOCK_ID,))
        return applied_now

    def execute(self, statement: str, parameters: Sequence[Any] = ()) -> int:
        """Execute one parameterized mutation in a transaction; never interpolate values."""
        with self.transaction() as connection:
            cursor = connection.execute(statement, parameters)
            return cursor.rowcount
