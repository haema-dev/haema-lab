-- 질문 처리(qa) 스키마. PostgreSQL 16 + pgvector 0.5 이상.
--
-- 적용: psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f qa/sql/schema.sql
-- Django 모델(qa/models.py)은 managed=False 로 이 테이블을 그대로 읽고 쓴다.
-- 스키마 변경은 이 파일이 원본이다. 모델은 이 파일을 따라간다.
--
-- 원칙
--   * 원본 문서 색인(source_document, document_chunk)과 생성된 답변(answer)은 다른 테이블이다.
--     생성된 답을 근거 문서로 다시 검색하지 않는다.
--   * 벡터는 두 곳에만 저장한다: document_chunk.embedding, answer.question_embedding.
--     답변 텍스트 자체는 벡터로 만들지 않는다.
--   * 차원 1024 = qwen3-embedding:0.6b 출력 차원. 임베딩 모델을 바꾸면 컬럼과 색인을 다시 만든다.

BEGIN;

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgcrypto;  -- gen_random_uuid() (PG13+는 내장이지만 명시)

-- ---------------------------------------------------------------------------
-- [사전 작업] 문서 색인
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS source_document (
    id              BIGSERIAL PRIMARY KEY,
    external_id     TEXT        NOT NULL UNIQUE,      -- 파일명 등 원본 식별자 (재색인 시 키)
    title           TEXT        NOT NULL DEFAULT '',
    source_uri      TEXT        NOT NULL DEFAULT '',
    content_sha256  CHAR(64)    NOT NULL,             -- 원문이 바뀌었을 때만 다시 임베딩
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS document_chunk (
    id               BIGSERIAL PRIMARY KEY,
    document_id      BIGINT       NOT NULL REFERENCES source_document(id) ON DELETE RESTRICT,
    chunk_index      INTEGER      NOT NULL CHECK (chunk_index >= 0),
    content          TEXT         NOT NULL,
    embedding        vector(1024) NOT NULL,
    embedding_model  TEXT         NOT NULL,
    created_at       TIMESTAMPTZ  NOT NULL DEFAULT now(),
    -- 원문이 바뀌면 기존 청크는 지우지 않고 retired 처리한다.
    -- 예전 답변의 인용(answer_citation)이 그대로 남아야 하고,
    -- retired 청크를 인용한 답변은 11번 유사 답변 재사용에서 빠진다.
    retired_at       TIMESTAMPTZ
);
-- 답변 인용 키는 'c' || id (예: [c42]). core/verify.py 의 CITATION_PATTERN 과 맞는다.

CREATE UNIQUE INDEX IF NOT EXISTS document_chunk_active_position
    ON document_chunk (document_id, chunk_index)
    WHERE retired_at IS NULL;

-- 검색은 항상 retired_at IS NULL 조건을 붙이므로 부분 HNSW 색인을 쓴다.
CREATE INDEX IF NOT EXISTS document_chunk_embedding_hnsw
    ON document_chunk USING hnsw (embedding vector_cosine_ops)
    WHERE retired_at IS NULL;

-- ---------------------------------------------------------------------------
-- 생성된 답변 (캐시). 원본 문서와 분리.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS answer (
    id                  BIGSERIAL PRIMARY KEY,
    answer_text         TEXT         NOT NULL,
    model_name          TEXT         NOT NULL,          -- 실제로 답을 만든 모델 (qwen 또는 fallback Gemini)
    is_fallback         BOOLEAN      NOT NULL DEFAULT false,
    verification        JSONB        NOT NULL,          -- 코드 검증 + 판정 결과
    question_embedding  vector(1024) NOT NULL,          -- 9번에서 만든 질문 벡터 (새 답변일 때만 저장)
    embedding_model     TEXT         NOT NULL,
    created_at          TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS answer_question_embedding_hnsw
    ON answer USING hnsw (question_embedding vector_cosine_ops);

CREATE TABLE IF NOT EXISTS answer_citation (
    answer_id  BIGINT  NOT NULL REFERENCES answer(id) ON DELETE CASCADE,
    chunk_id   BIGINT  NOT NULL REFERENCES document_chunk(id) ON DELETE RESTRICT,
    ordinal    INTEGER NOT NULL CHECK (ordinal >= 0),   -- 답변 안에서 인용된 순서
    PRIMARY KEY (answer_id, chunk_id)
);

CREATE INDEX IF NOT EXISTS answer_citation_chunk_idx ON answer_citation (chunk_id);

-- ---------------------------------------------------------------------------
-- 대화 메시지와 작업(job)
-- user_id 는 Gateway 가 인증 후 넘겨주는 값이다. 유저 테이블은 이 스키마 밖이라 FK 를 걸지 않는다.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS message (
    id          BIGSERIAL PRIMARY KEY,
    user_id     BIGINT      NOT NULL,
    role        TEXT        NOT NULL CHECK (role IN ('user', 'assistant')),
    content     TEXT,                                    -- role=user: 질문 원문
    answer_id   BIGINT      REFERENCES answer(id) ON DELETE RESTRICT,  -- role=assistant: 답변 참조
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT message_role_payload CHECK (
        (role = 'user'      AND content IS NOT NULL AND char_length(content) <= 300 AND answer_id IS NULL)
     OR (role = 'assistant' AND answer_id IS NOT NULL)
    )
);

CREATE INDEX IF NOT EXISTS message_user_created_idx ON message (user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS job (
    id                    UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id               BIGINT      NOT NULL,
    question_hash         CHAR(64)    NOT NULL,          -- sha256(정규화된 질문)
    status                TEXT        NOT NULL DEFAULT 'queued'
                          CHECK (status IN ('queued', 'running', 'succeeded', 'failed')),
    -- verification: 검증 실패 (유저 실패 카운터에 포함)
    -- infra:        모델 서버 5xx·연결 끊김·ErrorDeviceLost·Gemini 429 재시도 소진,
    --               큐 등록 실패, 워커 중단 (유저 실패 카운터에 포함하지 않음)
    failure_type          TEXT        CHECK (failure_type IN ('verification', 'infra')),
    failure_detail        TEXT        NOT NULL DEFAULT '',
    infra_retries         INTEGER     NOT NULL DEFAULT 0, -- 관측용. 재시도 횟수
    user_message_id       BIGINT      NOT NULL UNIQUE REFERENCES message(id) ON DELETE CASCADE,
    assistant_message_id  BIGINT      UNIQUE REFERENCES message(id) ON DELETE SET NULL,
    answer_id             BIGINT      REFERENCES answer(id) ON DELETE SET NULL,
    reused_answer         BOOLEAN     NOT NULL DEFAULT false,  -- 11번 유사 답변을 썼는지
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at            TIMESTAMPTZ,
    finished_at           TIMESTAMPTZ,
    CONSTRAINT job_failure_consistency CHECK (
        (status = 'failed') = (failure_type IS NOT NULL)
    ),
    CONSTRAINT job_success_has_answer CHECK (
        status <> 'succeeded' OR (answer_id IS NOT NULL AND assistant_message_id IS NOT NULL)
    )
);

-- 2번 중복 요청: 같은 유저 + 같은 질문 해시로 진행 중인 job 은 하나만.
-- 애플리케이션의 "조회 후 삽입"이 동시에 들어와도 DB 가 막는다.
CREATE UNIQUE INDEX IF NOT EXISTS job_one_active_per_question
    ON job (user_id, question_hash)
    WHERE status IN ('queued', 'running');

-- 오래 걸린 running job 회수(reap)와 유저 실패 카운터 조회용.
CREATE INDEX IF NOT EXISTS job_status_started_idx ON job (status, started_at);
CREATE INDEX IF NOT EXISTS job_user_failure_idx ON job (user_id, failure_type) WHERE status = 'failed';

COMMIT;
