from __future__ import annotations

import hashlib
import re
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from .database import PostgresDatabase

PAGE_MARKER = re.compile(r"^\s*=== PAGE (\d+) ===\s*$", re.MULTILINE)
TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.%-]*")


@dataclass(frozen=True)
class KnowledgeSource:
    title: str
    source_type: str
    source_uri: str
    content_sha256: str


@dataclass(frozen=True)
class KnowledgeChunk:
    chunk_id: str
    source_id: str
    ordinal: int
    page_start: int | None
    page_end: int | None
    content: str
    content_sha256: str


@dataclass(frozen=True)
class SearchHit:
    chunk_id: str
    title: str
    source_type: str
    source_uri: str
    page_start: int | None
    page_end: int | None
    content: str
    score: float

    @property
    def citation(self) -> str:
        pages = ""
        if self.page_start is not None:
            pages = f", p. {self.page_start}" if self.page_end == self.page_start else f", pp. {self.page_start}-{self.page_end}"
        return f"[{self.chunk_id[:12]}] {self.title}{pages}"


SCHEMA = """
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;
CREATE TABLE IF NOT EXISTS knowledge_sources (
    source_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    source_type TEXT NOT NULL,
    source_uri TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    ingested_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(source_uri, content_sha256)
);
CREATE TABLE IF NOT EXISTS knowledge_chunks (
    chunk_id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES knowledge_sources(source_id),
    ordinal INTEGER NOT NULL,
    page_start INTEGER,
    page_end INTEGER,
    content TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    UNIQUE(source_id, ordinal)
);
CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_chunks_fts USING fts5(
    chunk_id UNINDEXED, content, tokenize='porter unicode61'
);
"""


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _pages(text: str) -> list[tuple[int | None, str]]:
    matches = list(PAGE_MARKER.finditer(text))
    if not matches:
        return [(None, text)]
    pages: list[tuple[int | None, str]] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        pages.append((int(match.group(1)), text[match.end() : end]))
    return pages


def chunk_document(text: str, source_id: str, *, target_words: int = 260, overlap_words: int = 40) -> list[KnowledgeChunk]:
    if target_words < 20 or overlap_words < 0 or overlap_words >= target_words:
        raise ValueError("invalid chunk sizing")
    chunks: list[KnowledgeChunk] = []
    buffer: list[tuple[str, int | None]] = []
    ordinal = 0

    def emit() -> None:
        nonlocal buffer, ordinal
        content = " ".join(part.strip() for part, _ in buffer if part.strip())
        content = re.sub(r"\s+", " ", content).strip()
        if not content:
            buffer = []
            return
        page_values = [page for _, page in buffer if page is not None]
        content_hash = _digest(content)
        chunk_id = _digest(f"{source_id}:{ordinal}:{content_hash}")
        chunks.append(
            KnowledgeChunk(
                chunk_id, source_id, ordinal, min(page_values) if page_values else None,
                max(page_values) if page_values else None, content, content_hash,
            )
        )
        ordinal += 1
        words: list[tuple[str, int | None]] = []
        for part, page in buffer:
            words.extend((word, page) for word in part.split())
        tail = words[-overlap_words:] if overlap_words else []
        buffer = [(" ".join(word for word, _ in tail), tail[0][1])] if tail else []

    for page, page_text in _pages(text):
        paragraphs = re.split(r"\n\s*\n|(?<=[.!?])\s+(?=[A-Z])", page_text)
        for paragraph in paragraphs:
            paragraph = paragraph.strip()
            if not paragraph:
                continue
            if sum(len(part.split()) for part, _ in buffer) + len(paragraph.split()) > target_words and buffer:
                emit()
            buffer.append((paragraph, page))
    if buffer:
        emit()
    return chunks


class LocalKnowledgeIndex:
    """Deterministic, citation-preserving local index; it has no execution capability."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path)
        self.connection.executescript(SCHEMA)

    def close(self) -> None:
        self.connection.close()

    def ingest_text(self, title: str, source_type: str, source_uri: str, text: str) -> tuple[str, int]:
        if source_type not in {"book", "research_note", "exchange_document", "strategy_spec", "trade_journal"}:
            raise ValueError("unsupported knowledge source type")
        content_hash = _digest(text)
        source_id = str(uuid5(NAMESPACE_URL, f"{source_uri}:{content_hash}"))
        existing = self.connection.execute(
            "SELECT source_id FROM knowledge_sources WHERE source_uri=? AND content_sha256=?", (source_uri, content_hash)
        ).fetchone()
        if existing:
            count = self.connection.execute("SELECT count(*) FROM knowledge_chunks WHERE source_id=?", (existing[0],)).fetchone()[0]
            return existing[0], count
        chunks = chunk_document(text, source_id)
        with self.connection:
            self.connection.execute(
                "INSERT INTO knowledge_sources(source_id,title,source_type,source_uri,content_sha256) VALUES(?,?,?,?,?)",
                (source_id, title, source_type, source_uri, content_hash),
            )
            for chunk in chunks:
                self.connection.execute(
                    "INSERT INTO knowledge_chunks VALUES(?,?,?,?,?,?,?)",
                    (chunk.chunk_id, chunk.source_id, chunk.ordinal, chunk.page_start, chunk.page_end, chunk.content, chunk.content_sha256),
                )
                self.connection.execute("INSERT INTO knowledge_chunks_fts(chunk_id,content) VALUES(?,?)", (chunk.chunk_id, chunk.content))
        return source_id, len(chunks)

    def ingest_files(self, paths: Iterable[Path], source_type: str) -> dict[str, int]:
        result: dict[str, int] = {}
        for path in paths:
            _, count = self.ingest_text(path.stem, source_type, str(path.resolve()), path.read_text(errors="replace"))
            result[str(path)] = count
        return result

    def search(self, query: str, *, limit: int = 6, source_types: tuple[str, ...] = ()) -> list[SearchHit]:
        terms = TOKEN.findall(query)
        if not terms or limit < 1:
            return []
        expression = " OR ".join(f'"{term}"' for term in terms[:20])
        filters = ""
        parameters: list[object] = [expression]
        if source_types:
            filters = f" AND s.source_type IN ({','.join('?' for _ in source_types)})"
            parameters.extend(source_types)
        parameters.append(limit)
        rows = self.connection.execute(
            "SELECT c.chunk_id,s.title,s.source_type,s.source_uri,c.page_start,c.page_end,c.content,bm25(knowledge_chunks_fts) "
            "FROM knowledge_chunks_fts JOIN knowledge_chunks c USING(chunk_id) JOIN knowledge_sources s USING(source_id) "
            # Placeholder count is derived only from the source_types tuple; values remain parameterized.
            f"WHERE knowledge_chunks_fts MATCH ?{filters} ORDER BY bm25(knowledge_chunks_fts) LIMIT ?",  # nosec B608
            parameters,
        ).fetchall()
        return [SearchHit(*row[:-1], score=-float(row[-1])) for row in rows]

    def stats(self) -> dict[str, int]:
        sources = self.connection.execute("SELECT count(*) FROM knowledge_sources").fetchone()[0]
        chunks = self.connection.execute("SELECT count(*) FROM knowledge_chunks").fetchone()[0]
        return {"sources": sources, "chunks": chunks}


class PostgresKnowledgeIndex:
    """Read-only production retrieval over PostgreSQL's indexed search vector."""

    def __init__(self, database: PostgresDatabase) -> None:
        self.database = database

    def search(self, query: str, *, limit: int = 6, source_types: tuple[str, ...] = ()) -> list[SearchHit]:
        if not TOKEN.findall(query) or limit < 1:
            return []
        parameters: list[object] = [query, query]
        if source_types:
            parameters.append(list(source_types))
        parameters.append(limit)
        statement = (
            "SELECT c.chunk_id,s.title,s.source_type,s.source_uri,c.page_start,c.page_end,c.content,"
            "ts_rank_cd(c.search_vector,websearch_to_tsquery('english',%s)) AS score "
            "FROM knowledge_chunks c JOIN knowledge_sources s USING(source_id) "
            "WHERE c.search_vector @@ websearch_to_tsquery('english',%s) "
        )
        if source_types:
            statement += "AND s.source_type = ANY(%s) "
        statement += "ORDER BY score DESC,c.chunk_id LIMIT %s"
        with self.database.connect(autocommit=True) as connection:
            rows = connection.execute(statement, parameters).fetchall()
        return [SearchHit(*row[:-1], score=float(row[-1])) for row in rows]

    def stats(self) -> dict[str, int]:
        with self.database.connect(autocommit=True) as connection:
            sources = connection.execute("SELECT count(*) FROM knowledge_sources").fetchone()[0]
            chunks = connection.execute("SELECT count(*) FROM knowledge_chunks").fetchone()[0]
        return {"sources": sources, "chunks": chunks}
