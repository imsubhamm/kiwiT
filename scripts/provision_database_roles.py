"""Provision restricted runtime/reader roles using a migration-owner connection.

Writes generated URLs only to a protected local file; never prints credentials.
Apply once as an operator, then keep the migration URL out of service environments.
"""
import argparse
import os
import secrets
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

import psycopg
from psycopg import sql


def provision(connection, runtime_password, reader_password):
    for name, password in [('kiwit_runtime', runtime_password), ('kiwit_reader', reader_password)]:
        if connection.execute('SELECT 1 FROM pg_roles WHERE rolname=%s', (name,)).fetchone():
            raise ValueError('Role already exists; use an explicit rotation workflow')
        connection.execute(sql.SQL('CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT').format(
            sql.Identifier(name), sql.Literal(password)))
        connection.execute(sql.SQL('GRANT USAGE ON SCHEMA public TO {}').format(sql.Identifier(name)))
        connection.execute(sql.SQL('GRANT SELECT ON ALL TABLES IN SCHEMA public TO {}').format(sql.Identifier(name)))
    grant_runtime(connection)


def grant_runtime(connection):
    """Run again after migrations. Deliberately does not grant ownership or DDL."""
    for (table,) in connection.execute("SELECT tablename FROM pg_tables WHERE schemaname='public'").fetchall():
        if table == 'schema_migrations':
            continue
        operations = 'INSERT' if table in {'audit_events', 'risk_decisions', 'banknifty_events'} else 'INSERT, UPDATE, DELETE'
        connection.execute(sql.SQL('GRANT ' + operations + ' ON TABLE public.{} TO kiwit_runtime').format(sql.Identifier(table)))
    connection.execute('GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO kiwit_runtime')
    for role in ('kiwit_runtime', 'kiwit_reader'):
        connection.execute(sql.SQL('GRANT SELECT ON ALL TABLES IN SCHEMA public TO {}').format(sql.Identifier(role)))
        connection.execute(sql.SQL('REVOKE CREATE ON SCHEMA public FROM {}').format(sql.Identifier(role)))
    # PostgreSQL 15+ defaults deny PUBLIC schema creation. Fail instead of altering shared permissions.
    for role in ('kiwit_runtime', 'kiwit_reader'):
        if connection.execute("SELECT has_schema_privilege(%s,'public','CREATE')", (role,)).fetchone()[0]:
            raise ValueError('PUBLIC grants schema CREATE; operator must revoke this for a dedicated application schema')


def with_credentials(url, username, password):
    parts = urlsplit(url)
    host = parts.hostname
    if ':' in host:
        host = '[' + host + ']'
    return urlunsplit((parts.scheme, quote(username) + ':' + quote(password) + '@' + host +
                      (':' + str(parts.port) if parts.port else ''), parts.path, parts.query, parts.fragment))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path)
    parser.add_argument('--refresh-grants', action='store_true')
    args = parser.parse_args()
    url = os.environ['KIWIT_MIGRATION_DATABASE_URL']
    with psycopg.connect(url) as connection:
        if args.refresh_grants:
            grant_runtime(connection)
        else:
            if not args.output:
                parser.error('--output required; keep this file outside the repository')
            # Exclusive creation protects existing configuration from accidental overwrite.
            fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            runtime, reader = secrets.token_urlsafe(40), secrets.token_urlsafe(40)
            try:
                provision(connection, runtime, reader)
                with os.fdopen(fd, 'w') as handle:
                    handle.write('KIWIT_DATABASE_URL=' + with_credentials(url, 'kiwit_runtime', runtime) + '\n')
                    handle.write('KIWIT_READONLY_DATABASE_URL=' + with_credentials(url, 'kiwit_reader', reader) + '\n')
            except BaseException:
                args.output.unlink(missing_ok=True)
                raise
    print('Database roles/grants prepared; credentials were not printed')


if __name__ == '__main__':
    main()
