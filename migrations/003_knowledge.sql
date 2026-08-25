BEGIN;

CREATE TABLE IF NOT EXISTS knowledge_sources (
    source_id           uuid PRIMARY KEY,
    title               text NOT NULL,
    source_type         text NOT NULL CHECK (source_type IN ('book', 'research_note', 'exchange_document', 'strategy_spec', 'trade_journal')),
    source_uri          text NOT NULL,
    content_sha256      char(64) NOT NULL,
    effective_from      timestamptz,
    effective_to        timestamptz,
    metadata            jsonb NOT NULL DEFAULT '{}'::jsonb,
    ingested_at         timestamptz NOT NULL DEFAULT now(),
    UNIQUE (source_uri, content_sha256),
    CHECK (effective_to IS NULL OR effective_from IS NULL OR effective_to >= effective_from)
);

CREATE TABLE IF NOT EXISTS knowledge_chunks (
    chunk_id            char(64) PRIMARY KEY,
    source_id           uuid NOT NULL REFERENCES knowledge_sources(source_id),
    ordinal             integer NOT NULL CHECK (ordinal >= 0),
    page_start          integer,
    page_end            integer,
    content             text NOT NULL CHECK (length(content) > 0),
    content_sha256      char(64) NOT NULL,
    search_vector       tsvector GENERATED ALWAYS AS (to_tsvector('english', content)) STORED,
    metadata            jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at          timestamptz NOT NULL DEFAULT now(),
    UNIQUE (source_id, ordinal),
    CHECK (page_start IS NULL OR page_start > 0),
    CHECK (page_end IS NULL OR page_end >= page_start)
);

CREATE INDEX IF NOT EXISTS knowledge_chunks_search_idx ON knowledge_chunks USING gin(search_vector);
CREATE INDEX IF NOT EXISTS knowledge_chunks_source_idx ON knowledge_chunks (source_id, ordinal);
CREATE INDEX IF NOT EXISTS knowledge_sources_effective_idx ON knowledge_sources (source_type, effective_from, effective_to);

INSERT INTO schema_migrations(version, name) VALUES (3, 'knowledge')
ON CONFLICT (version) DO UPDATE SET name = EXCLUDED.name;
COMMIT;
