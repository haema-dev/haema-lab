# 0001. 질문 처리(qa) ↔ 모델 게이트웨이 계약 정리

## 제약
- 스토리보드: 모델 호출은 워커 → FastAPI(`/embed`, `/generate`, `/judge`, `/fallback`)만. 큐는 요청당 1회.
- 인프라 실패(5xx, 연결 끊김, ErrorDeviceLost, Gemini 429)만 재시도하고 유저 실패 카운터에 넣지 않는다.
- FastAPI는 DB 자격증명을 갖지 않는다.

## 선택지
1. Django 클라이언트를 FastAPI 스키마에 맞춘다 (`inputs`, `truncated`, `error.retryable`).
2. FastAPI 스키마를 Django에 맞춘다 (`texts`, `done_reason`).

## 측정 (로컬 컨테이너, 가짜 Ollama/Gemini. 실제 모델·실제 서버 아님)
- 기존 Django 요청 `{"kind": "query", "texts": [...]}` → 게이트웨이 422 (`inputs` 필드 없음). 모든 job이 9단계에서 실패.
- `/fallback`은 `finish_reason`만 주므로 기존 클라이언트는 잘린 fallback 답을 못 잡음.
- 빈 `contexts` → 422.
- 수정 후 E2E (PostgreSQL 16 + pgvector, Redis 7, 실제 FastAPI 앱, 가짜 업스트림): 202 → running → succeeded,
  중복 요청은 같은 job_id, 301자는 400, 비슷한 질문은 재사용(업스트림 생성 호출 없음).

## 결정
- 1번. FastAPI `src/ai/schemas.py`가 계약 원본. FastAPI는 테스트가 이미 그 계약으로 되어 있음.
- 재시도 여부는 게이트웨이의 `error.retryable`을 따른다. 본문이 없을 때만 상태 코드(429/5xx)로 판단.
- 근거 0개면 생성하지 않고 `failed / infra / no_evidence`로 끝낸다 (색인 문제이지 유저 탓이 아님).
- 동기 `/api/rag/chat`(API 프로세스가 Ollama 직접 호출)은 큐·게이트웨이·저장된 검증을 건너뛰므로 라우트를 뺀다.
- FastAPI 프로젝트 안의 Django 앱(`config/`, `rag/`, `manage.py`, DB 접속 설정 포함)은 원칙 위반이고 이미지에도 안 들어가므로 뺀다.
