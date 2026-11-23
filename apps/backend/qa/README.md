# qa — 질문 처리 (Django API + 워커)

> **커밋 푸시 하지말고 코드는 파일로 만들어줘**
> 이 앱의 코드는 파일로만 전달한다. 커밋·푸시·PR 생성은 하지 않는다. (루트 `CLAUDE.md` Git 규칙과 같음)

## 파일

| 파일 | 역할 |
| -- | -- |
| `sql/schema.sql` | 테이블 설계 원본 (PostgreSQL + pgvector) |
| `models.py` | 스키마를 그대로 읽고 쓰는 Django 모델 (`managed = False`) |
| `migrations/0002_apply_schema_sql.py` | `manage.py migrate` 때 `schema.sql` 실행 (PostgreSQL만) |
| `repository.py` | **DB 읽기/쓰기 전부** (3, 7, 19, 20번 쓰기 + 조회·검색) |
| `fastapi_client.py` | **FastAPI 호출** (`/embed`, `/generate`, `/judge`, `/fallback`) + 인프라 실패 재시도 |
| `redis_queue.py` | Redis 큐(job_id), 완료 알림(pub/sub), GPU 세마포어 |
| `views.py`, `urls.py` | `POST /api/qa/questions` (1~5번), `GET /api/qa/jobs/<job_id>` (폴링) |
| `worker.py` | job 하나를 끝까지 처리 (6~20번) |
| `management/commands/run_qa_worker.py` | 워커 프로세스 |
| `management/commands/index_documents.py` | 문서 색인 배치 (0번). `*.md`, `*.txt`, `official_events` JSON(`data/*.json`, 이벤트 하나 = 문단 하나) |
| `tests.py` | PostgreSQL 필요. FastAPI는 mock, Redis 호출은 patch |

## 실행

```bash
# 환경 변수 (비밀값은 레포에 넣지 않는다)
export POSTGRES_HOST=... POSTGRES_PASSWORD=... DJANGO_SECRET_KEY=...
export REDIS_URL=redis://:password@redis:6379/0 FASTAPI_BASE_URL=http://fastapi:8000

uv run python manage.py migrate                    # schema.sql 적용 (또는 psql -f qa/sql/schema.sql)
uv run python manage.py index_documents ./data     # 0. 문서 색인
uv run python manage.py run_qa_worker              # 워커 (API와 같은 이미지, 별도 Deployment)
uv run python manage.py test qa                    # POSTGRES_HOST 없으면 DB 테스트는 skip
```

## API

```http
POST /api/qa/questions        X-User-Id: 1      {"question": "8월 기준금리는?"}
→ 202 {"job_id": "..."}                         # 중복이면 {"job_id": "...", "duplicate": true}
→ 400 (300자 초과: DB·큐·FastAPI 모두 건드리지 않음) / 401 / 503 (큐 등록 실패)

GET /api/qa/jobs/<job_id>     X-User-Id: 1
→ {"status": "queued|running|succeeded|failed", "answer": {...}, "failure_type": "..."}
```

`X-User-Id`는 Gateway가 인증 후 넣어 준다고 가정한다 (Gateway 쪽 구현은 아직 없음).

## FastAPI 계약

원본은 FastAPI 쪽 `src/ai/schemas.py`다. `fastapi_client.py` 상단 docstring에 같은 내용을 적어 두었다.
프롬프트는 FastAPI가 만든다. Django는 질문과 근거(`{"id": "c42", "text": ...}`)만 보낸다.

* `/embed` 요청 필드는 `inputs`다 (`texts` 아님). 응답 벡터가 1024차원이 아니면 계약 오류로 본다.
* 답변은 `[c42]` 형태로 인용해야 `core/verify.py`가 인용을 검사할 수 있다.
* 잘린 답변 판정은 응답의 `truncated`로 한다. `/generate`는 Ollama `done_reason`, `/fallback`은 Gemini `finish_reason`(`MAX_TOKENS`)을 주므로 이름이 달라도 `truncated`는 둘 다 있다.
* 근거 없음 답변은 FastAPI 프롬프트의 `REFUSAL_TEXT`와 `core/verify.py`의 `REFUSAL_TEXT`가 같아야 인용 없이 통과한다.
* 오류 응답 `{"error": {"retryable": ...}}`을 따른다. `retryable: true`(503)만 인프라 실패로 재시도하고, `not_configured`·`bad_output`·`upstream_rejected`는 재시도하지 않는다. `Retry-After`(Gemini 429)가 있으면 그만큼은 기다린다.
* `contexts`는 비어 있으면 안 된다. 그래서 12번 검색 결과가 0개면 생성 없이 `failed`(`infra`, `no_evidence`)로 끝낸다.

## 설계 메모

* 중복 요청은 앱에서 먼저 조회하고, 동시에 들어온 경우는 부분 유니크 색인 `job_one_active_per_question`이 막는다.
* 문서가 바뀌면 기존 청크는 지우지 않고 `retired_at`을 채운다. 예전 답변의 인용이 남고, retired 청크를 인용한 답변은 유사 답변 재사용에서 빠진다.
* 워커가 죽어 `running`/`queued`로 남은 job은 워커가 1분마다 `infra` 실패로 정리한다 (안 하면 같은 질문이 영원히 중복으로 막힌다).
* `reap` 이후 늦게 끝난 워커의 결과는 트랜잭션째 롤백된다.
* `reap`으로 `failed`가 된 job도 커밋 후 Redis "job 끝남" 알림을 보낸다 (SSE가 영원히 기다리지 않게).
* 12번 근거 검색: 거리순으로 보면서 남은 예산(2,000자)에 들어가는 청크만 담는다. 안 들어가는 청크는 건너뛰고 더 짧은 다음 청크는 담을 수 있다. 1위 청크 혼자 예산을 넘을 때만 잘라서 쓴다.
* JSON 색인은 출처 URL을 넣지 않는다. 이벤트 하나에 약 170자(프롬프트 처리 약 70 tok/s)를 쓰고 숫자 근거가 아니기 때문이다. 금리 범위는 `3.75% ~ 4.00%`처럼 양쪽에 `%`를 붙인다 (`core/verify.py`는 단위 없는 숫자를 읽지 않는다).

## 추정값 (측정 필요)

* `QA_ANSWER_REUSE_MAX_DISTANCE=0.08` — 유사 답변 재사용 기준 코사인 거리. 실제 질문 쌍으로 측정 전.
* `FASTAPI_INFRA_RETRY_DELAYS=5,10,20` — `ErrorDeviceLost` 복구 약 15초(1회 관측)를 넘기도록 잡은 값.
* `QA_JOB_RUNNING_TIMEOUT_SECONDS=3600`, `QA_JOB_QUEUED_TIMEOUT_SECONDS=7200` — stale job 판정.
* `QA_CHUNK_MAX_CHARS=400` — 근거 2,000자 / 5개.
* `index_documents --batch-size 16` — FastAPI `embed_timeout` 10초 안에 문서 16개 임베딩이 끝난다고 가정. 실측 전 (질문 1개 임베딩만 약 0.65초 측정).
