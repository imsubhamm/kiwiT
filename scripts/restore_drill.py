"""Logical backup/restore verification into an empty isolated database; never PITR proof.

Set KIWIT_BACKUP_SOURCE_URL and KIWIT_RESTORE_TARGET_URL. The target must already
exist and contain no user relations. This tool never drops databases or tables.
Operator-only CLI: PostgreSQL tools run with explicit argv and no shell.
"""
import argparse
import hashlib
import json
import os
import subprocess  # nosec B404
import time
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict


def pg_env(url):
    names = {'dbname': 'PGDATABASE', 'user': 'PGUSER', 'host': 'PGHOST',
             'hostaddr': 'PGHOSTADDR', 'port': 'PGPORT', 'sslmode': 'PGSSLMODE',
             'sslrootcert': 'PGSSLROOTCERT', 'channel_binding': 'PGCHANNELBINDING',
             'connect_timeout': 'PGCONNECT_TIMEOUT', 'options': 'PGOPTIONS'}
    # PGPASSWORD is a libpq environment-variable name, not an embedded credential.
    names['password'] = 'PGPASSWORD'  # nosec B105
    settings = conninfo_to_dict(url)
    if settings.keys() - names.keys():
        raise ValueError('Unsupported connection option; extend explicit libpq environment mapping')
    return {**{k: v for k, v in os.environ.items() if not k.startswith('PG')},
            **{names[k]: v for k, v in settings.items()}}


def pg_tools(directory):
    """Require an explicit installed tool directory; never search PATH for executables."""
    if not directory.is_absolute():
        raise ValueError('--pg-bin must be an absolute trusted PostgreSQL installation path')
    tools = [(directory / name).resolve(strict=True) for name in ('pg_dump', 'pg_restore')]
    if any(not path.is_file() or not os.access(path, os.X_OK) for path in tools):
        raise ValueError('PostgreSQL tools must be executable regular files')
    return tuple(map(str, tools))


def manifest(connection):
    connection.execute("SET LOCAL TIME ZONE 'UTC'")
    result = {}
    tables = connection.execute("SELECT schemaname,tablename FROM pg_tables WHERE schemaname "
                                "NOT IN ('pg_catalog','information_schema') ORDER BY 1,2").fetchall()
    for schema, table in tables:
        digest = hashlib.sha256()
        count = 0
        query = sql.SQL('SELECT to_jsonb(t)::text FROM {}.{} t ORDER BY to_jsonb(t)::text COLLATE "C"').format(
            sql.Identifier(schema), sql.Identifier(table))
        with connection.cursor(name='drill_rows') as cursor:
            cursor.execute(query)
            for (row,) in cursor:
                digest.update(row.encode() + b'\n')
                count += 1
        result[f'{schema}.{table}'] = {'rows': count, 'sha256': digest.hexdigest()}
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True, help='New private artifact directory')
    parser.add_argument('--pg-bin', type=Path, default=Path('/usr/bin'))
    args = parser.parse_args()
    pg_dump, pg_restore = pg_tools(args.pg_bin)
    args.output = args.output.resolve()
    source = os.environ['KIWIT_BACKUP_SOURCE_URL']
    target = os.environ['KIWIT_RESTORE_TARGET_URL']
    if source == target:
        raise ValueError('Source and target must differ')
    with psycopg.connect(target) as connection:
        if connection.execute("SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                              "WHERE n.nspname NOT IN ('pg_catalog','information_schema') "
                              "AND n.nspname NOT LIKE 'pg_toast%' AND c.relkind IN ('r','p','v','m','S')").fetchone()[0]:
            raise ValueError('Restore target is not empty')
    args.output.mkdir(mode=0o700, parents=False, exist_ok=False)
    archive = args.output / 'backup.dump'
    started = time.monotonic()
    with psycopg.connect(source) as connection:
        connection.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
        snapshot = connection.execute('SELECT pg_export_snapshot()').fetchone()[0]
        before = manifest(connection)
        # Validated operator-selected executable; fixed options and separate argv, never shell input.
        subprocess.run([pg_dump, '--format=custom', '--no-owner', '--no-acl',  # nosec B603
                        '--snapshot', snapshot, '--file', str(archive)],
                       env=pg_env(source), check=True, shell=False)
    archive.chmod(0o600)
    # The archive is an absolute path created above; credentials stay out of argv.
    subprocess.run([pg_restore, '--exit-on-error', '--single-transaction',  # nosec B603
                    '--no-owner', '--no-acl', '--dbname', '', str(archive)],
                   env=pg_env(target), check=True, shell=False)
    with psycopg.connect(target) as connection:
        after = manifest(connection)
    with archive.open('rb') as archived:
        archive_digest = hashlib.file_digest(archived, 'sha256').hexdigest()
    result = {'kind': 'logical_restore_drill', 'pitr_verified': False,
              'elapsed_seconds': round(time.monotonic() - started, 2), 'match': before == after,
              'source_manifest': before, 'restored_manifest': after,
              'archive_sha256': archive_digest}
    (args.output / 'result.json').write_text(json.dumps(result, indent=2))
    print(json.dumps({'match': result['match'], 'tables': len(before), 'output': str(args.output)}))
    if not result['match']:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
