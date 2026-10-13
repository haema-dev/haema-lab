CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS chat_conversation (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES auth_user(id) ON DELETE CASCADE,
    title VARCHAR(200) NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_chat_conversation_user_updated
    ON chat_conversation (user_id, updated_at DESC);


CREATE TABLE IF NOT EXISTS chat_message (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    conversation_id BIGINT NOT NULL
        REFERENCES chat_conversation(id) ON DELETE CASCADE,
    user_id BIGINT
        REFERENCES auth_user(id) ON DELETE SET NULL,
    question VARCHAR(300),
    answer TEXT,
    status VARCHAR(16) NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'processing', 'done', 'failed', 'rejected')),
    source VARCHAR(16)
        CHECK (source IN ('cache', 'generated', 'fallback')),
    cited_chunk_ids BIGINT[],
    verification JSONB,
    retry_count SMALLINT NOT NULL DEFAULT 0,
    error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_chat_message_conversation
    ON chat_message (conversation_id);

CREATE INDEX IF NOT EXISTS idx_chat_message_user_created
    ON chat_message (user_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_chat_message_processing
    ON chat_message (status)
    WHERE status IN ('pending', 'processing');


CREATE TABLE IF NOT EXISTS source_document (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    url TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL DEFAULT '',
    published_at DATE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);


CREATE TABLE IF NOT EXISTS source_chunk (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    document_id BIGINT NOT NULL
        REFERENCES source_document(id) ON DELETE CASCADE,
    chunk_index INTEGER NOT NULL,
    content TEXT NOT NULL,
    embedding vector(1024),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_source_chunk_document_index
        UNIQUE (document_id, chunk_index)
);

CREATE INDEX IF NOT EXISTS idx_source_chunk_embedding
    ON source_chunk
    USING hnsw (embedding vector_cosine_ops);


CREATE TABLE IF NOT EXISTS answer_cache (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    question VARCHAR(300) NOT NULL,
    question_embedding vector(1024),
    answer TEXT NOT NULL,
    cited_chunk_ids BIGINT[],
    verification JSONB,
    hit_count INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_answer_cache_embedding
    ON answer_cache
    USING hnsw (question_embedding vector_cosine_ops);