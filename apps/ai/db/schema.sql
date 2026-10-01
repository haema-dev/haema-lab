-- haema-lab PostgreSQL 스키마
-- 전제: Django 기본 auth_user 테이블이 이미 있다 (migrate 후). 유저/요청 제한 단위는 auth_user.
-- 임베딩 차원 1024 = qwen3-embedding:0.6b 기본 출력 차원 (실제 모델 출력으로 확인 필요).
-- 주의: Django ORM으로 관리한다면 이 SQL 대신 models.py + migrate를 쓰고, 이 파일은 참고용/수동 초기화용.

CREATE EXTENSION IF NOT EXISTS vector;

-- 1. 대화 -----------------------------------------------------------------
CREATE TABLE chat_conversation (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id     INTEGER     NOT NULL REFERENCES auth_user (id) ON DELETE CASCADE,
    title       VARCHAR(200) NOT NULL DEFAULT '',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_conversation_user_updated ON chat_conversation (user_id, updated_at DESC);

-- 2. 메시지 (질문 1개 + 답변 1개 = 1행) ---------------------------------------
CREATE TABLE chat_message (
    id                BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    conversation_id   BIGINT      NOT NULL REFERENCES chat_conversation (id) ON DELETE CASCADE,
    user_id           INTEGER     NOT NULL REFERENCES auth_user (id) ON DELETE CASCADE,  -- 요청 제한 집계용(비정규화)
    question          VARCHAR(300) NOT NULL,                       -- 질문 300자 상한
    answer            TEXT,                                        -- 완료 전 NULL
    status            VARCHAR(16) NOT NULL DEFAULT 'pending'
                      CHECK (status IN ('pending', 'processing', 'done', 'failed', 'rejected')),
    source            VARCHAR(16)                                  -- 답변 출처: 캐시 / 생성 / 폴백
                      CHECK (source IN ('cache', 'generated', 'fallback')),
    cited_chunk_ids   BIGINT[]    NOT NULL DEFAULT '{}',           -- source_chunk.id 목록
    verification      JSONB,                                       -- 코드 검증(수치/인용) + Gemini 판정 결과
    retry_count       SMALLINT    NOT NULL DEFAULT 0,              -- 인프라 실패 재시도 (유저 실패 카운터와 별개)
    error             TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at      TIMESTAMPTZ
);
CREATE INDEX idx_message_conversation ON chat_message (conversation_id, created_at);
CREATE INDEX idx_message_user_created ON chat_message (user_id, created_at DESC);  -- 요청 제한 조회
CREATE INDEX idx_message_status       ON chat_message (status) WHERE status IN ('pending', 'processing');

-- 3. 원본 문서 색인 (RAG 근거) — 배치로만 채운다 -----------------------------------
CREATE TABLE source_document (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    url           TEXT        NOT NULL UNIQUE,
    title         TEXT        NOT NULL DEFAULT '',
    published_at  DATE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE source_chunk (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    document_id   BIGINT      NOT NULL REFERENCES source_document (id) ON DELETE CASCADE,
    chunk_index   INTEGER     NOT NULL,
    content       TEXT        NOT NULL,
    embedding     vector(1024) NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (document_id, chunk_index)
);
CREATE INDEX idx_source_chunk_embedding ON source_chunk
    USING hnsw (embedding vector_cosine_ops);

-- 4. 답변 캐시 — 근거 검색 대상이 아니다 (source_chunk와 분리) ---------------------
CREATE TABLE answer_cache (
    id                BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    question          VARCHAR(300) NOT NULL,
    question_embedding vector(1024) NOT NULL,
    answer            TEXT        NOT NULL,                        -- 검증 통과한 답변만 저장
    cited_chunk_ids   BIGINT[]    NOT NULL DEFAULT '{}',
    verification      JSONB,
    hit_count         INTEGER     NOT NULL DEFAULT 0,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_hit_at       TIMESTAMPTZ
);
CREATE INDEX idx_answer_cache_embedding ON answer_cache
    USING hnsw (question_embedding vector_cosine_ops);
