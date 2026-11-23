# 0002. 질문 처리 경로를 `chat` 하나로 합친다

## 제약
- CLAUDE.md: 답변과 리포트는 같은 코드 검증(`core/verify.py`)을 통과해야 한다. 수치는 코드가 계산하고 LLM 은 해석만 한다.
- 모델 게이트웨이(apps/ai)에 있는 엔드포인트는 `/embed`, `/generate` 뿐이다. `/judge`, `/fallback` 은 없다.

## 현황 (2026-11-24, 코드 기준)
- `chat`: `INSTALLED_APPS` 와 `/api/chat/` 에 연결됨. 워커 매니페스트(`run_worker`)도 이 앱을 쓴다. 현재 게이트웨이 계약과 맞다.
- `qa`: 설치·라우팅되지 않음. 0001 의 계약(`src/ai/schemas.py`, `/judge`, `/fallback`, `error.retryable`)을 전제로 하는데
  그 게이트웨이 코드는 레포에 없다. 큐·워커·클라이언트·모델이 `chat` 과 중복.
- 검증 코드가 `core/verify.py`(qa 용)와 `chat/verify.py`(chat 용)에 로직째 복사돼 있었다.
- 근거 문서가 0개면 `chat` 이 근거 없이 생성한 답을 검증 없이 `done` 으로 저장했다 (0001 의 결정과도 반대).
- `/generate` 가 Ollama `done_reason` 을 버려서 README 의 완결성 검사(잘린 답 거절)를 할 수 없었다.

## 결정
1. `qa` 앱을 삭제한다. 필요한 아이디어(Gemini 판정, 인용 테이블)는 게이트웨이에 `/judge` 가 생길 때 `chat` 에 다시 만든다.
2. 검증은 `core/verify.py` 하나만 둔다 (`chat/verify.py` 삭제). `chat` 은 `check_generation` 을 쓴다.
3. `/generate` 응답에 `truncated` (= Ollama `done_reason == "length"`) 를 넣는다. Django 는 이 값이 없으면 거절한다
   (없을 때 `False` 로 가정하면 잘린 답이 검사를 통과하므로).
4. 근거 문서가 0개면 생성하지 않고 `failed / no_evidence` 로 닫는다. 재시도하지 않는다 (색인 문제라 같은 결과).

## 검증
- mock 테스트: `apps/ai/tests/test_api.py` (Ollama 는 respx), `core/test_verify.py`, `chat/test_fastapi_client.py` (httpx MockTransport).
- DB 테스트(`chat/test_pipeline.py`, `chat/test_worker.py`)는 PostgreSQL + pgvector 가 필요해 `.github/workflows/test.yml` 에서 돈다.
