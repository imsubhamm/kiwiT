from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from kiwit.database import DatabaseSettings, PostgresDatabase


def main() -> None:
    parser = argparse.ArgumentParser(description="Synchronize the local content-addressed knowledge index to PostgreSQL")
    parser.add_argument("--source", default="data/local/kiwit_knowledge.sqlite3")
    args = parser.parse_args()

    local = sqlite3.connect(ROOT / args.source)
    local.row_factory = sqlite3.Row
    sources = local.execute(
        "SELECT source_id,title,source_type,source_uri,content_sha256 FROM knowledge_sources ORDER BY source_id"
    ).fetchall()
    chunks = local.execute(
        "SELECT chunk_id,source_id,ordinal,page_start,page_end,content,content_sha256 FROM knowledge_chunks ORDER BY source_id,ordinal"
    ).fetchall()
    local.close()

    database = PostgresDatabase(DatabaseSettings.from_env())
    with database.transaction() as connection:
        with connection.cursor() as cursor:
            cursor.executemany(
                "INSERT INTO knowledge_sources(source_id,title,source_type,source_uri,content_sha256) VALUES(%s,%s,%s,%s,%s) "
                "ON CONFLICT(source_uri,content_sha256) DO NOTHING",
                [tuple(row) for row in sources],
            )
            cursor.executemany(
                "INSERT INTO knowledge_chunks(chunk_id,source_id,ordinal,page_start,page_end,content,content_sha256) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(chunk_id) DO NOTHING",
                [tuple(row) for row in chunks],
            )
        production_sources = connection.execute("SELECT count(*) FROM knowledge_sources").fetchone()[0]
        production_chunks = connection.execute("SELECT count(*) FROM knowledge_chunks").fetchone()[0]
    print(
        json.dumps(
            {
                "status": "ok",
                "local_sources": len(sources),
                "local_chunks": len(chunks),
                "production_sources": production_sources,
                "production_chunks": production_chunks,
            }
        )
    )


if __name__ == "__main__":
    main()
