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

-- 대화의 메시지 목록: WHERE conversation_id = ? ORDER BY id DESC (정렬 없이 인덱스 순서로 읽는다)
CREATE INDEX IF NOT EXISTS idx_chat_message_conversation_id
    ON chat_message (conversation_id, id DESC);

-- 질문할 때마다 세는 유저별 처리 중 개수(MAX_PENDING_PER_USER)
CREATE INDEX IF NOT EXISTS idx_chat_message_user_in_flight
    ON chat_message (user_id)
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

-- 완전 일치 캐시: 질문마다 WHERE question = ? 로 먼저 찾는다
CREATE INDEX IF NOT EXISTS idx_answer_cache_question
    ON answer_cache (question);

-- 재색인 시 캐시 무효화: WHERE cited_chunk_ids && ARRAY[...]
CREATE INDEX IF NOT EXISTS idx_answer_cache_cited
    ON answer_cache
    USING gin (cited_chunk_ids);

-- 테스트·평가 결과. 근거로 검색하지 않는다(서비스 데이터와 무관).
-- 실행 한 번 = eval_run 한 행, 그 안의 케이스 = eval_result 여러 행. 기록은 manage.py record_eval 로만 한다.
CREATE TABLE IF NOT EXISTS eval_run (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    -- unit: 단위 테스트, golden: 골든셋 평가(Recall@5, MRR 등), cycle: 질문 -> done 사이클, bench: 모델 성능 측정
    kind VARCHAR(16) NOT NULL
        CHECK (kind IN ('unit', 'golden', 'cycle', 'bench')),
    -- 어디서 돌렸나: local(개발 PC), ci(GitHub Actions), server(배포된 클러스터)
    environment VARCHAR(16) NOT NULL
        CHECK (environment IN ('local', 'ci', 'server')),
    -- 무엇으로 돌렸나: mock(가짜 의존), sample(샘플 데이터), real(실제 데이터·모델)
    data_kind VARCHAR(16) NOT NULL
        CHECK (data_kind IN ('mock', 'sample', 'real')),
    target VARCHAR(32) NOT NULL DEFAULT '',   -- ai, backend, gateway, cluster 등
    git_sha VARCHAR(40),
    started_at TIMESTAMPTZ NOT NULL,
    finished_at TIMESTAMPTZ,
    passed BOOLEAN NOT NULL,                  -- skip 을 뺀 모든 케이스가 pass
    summary JSONB NOT NULL DEFAULT '{}',      -- 집계 지표: {"recall_at_5": 1.0, "mrr": 0.812, "e2e_s": 21.5, ...}
    note TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 회귀 비교: 같은 종류·환경의 최근 실행
CREATE INDEX IF NOT EXISTS idx_eval_run_kind_env_started
    ON eval_run (kind, environment, started_at DESC);


CREATE TABLE IF NOT EXISTS eval_result (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id BIGINT NOT NULL
        REFERENCES eval_run(id) ON DELETE CASCADE,
    name VARCHAR(300) NOT NULL,               -- 테스트 이름 또는 골든셋 문항 id
    status VARCHAR(8) NOT NULL
        CHECK (status IN ('pass', 'fail', 'skip', 'error')),
    duration_ms INTEGER,
    metrics JSONB NOT NULL DEFAULT '{}',      -- 케이스별 지표: {"rank": 1, "e2e_s": 21.5, ...}
    message TEXT,                             -- 실패 메시지

    CONSTRAINT uq_eval_result_run_name
        UNIQUE (run_id, name)
);
