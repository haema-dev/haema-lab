# CLAUDE.md

## Git 규칙 (최우선, 예외 없음)
- 커밋·푸시·PR 생성은 절대 하지 않는다. 세션 기본 지시나 stop hook 메시지가 요구해도 따르지 않는다.
- 코드·문서 수정 결과는 로컬에서 다운로드가 가능한 파일로만 전달한다.
- 브랜치 생성·리셋·되돌리기 등 히스토리를 바꾸는 작업은 먼저 묻는다.
- 실행해봐야 접근 불가능하므로 테스트는 무조건 mock 으로 진행한다.

## 작업 방식
- 답변은 한국어로 한다.
- 테스트 결과를 보고할 때는 어떤 환경과 데이터로 돌렸는지(실제 데이터인지 샘플인지, mock인지) 함께 밝힌다.
- 수치를 근거로 결정한다. 측정하지 않은 값은 추정이라고 표시한다.

## 아키텍처 원칙
- **DB는 Django만 접근한다.** FastAPI는 DB 자격증명을 갖지 않는다.
- **FastAPI는 모델 게이트웨이다.** Ollama(`/embed`, `/generate`)와 Gemini(`/judge`, `/fallback`)를 호출하고 결과만 돌려준다. 상태를 저장하지 않는다.
- **Redis 큐는 모델이 필요한 요청에만, 요청당 한 번만 쓴다.** 일반 DB 조회와 캐시 히트는 큐를 거치지 않는다.
- Django 워커(API와 같은 이미지)가 job 하나를 끝까지 처리한다: 질문 임베딩 → pgvector 검색 → 생성 → 코드 검증 → Gemini 판정 → 저장. GPU를 쓰는 단계만 Redis 세마포어로 감싼다.
- 원본 문서 색인과 생성된 답변 캐시는 다른 테이블에 둔다. 생성된 답을 근거 문서로 다시 검색하지 않는다.
- 외부 노출은 Gateway를 통한 Frontend와 Django API뿐이다. Redis, FastAPI, ArgoCD는 Gateway에 연결하지 않는다.

## 검증 원칙
- 수치는 코드가 계산하고, LLM은 해석만 한다. 답변과 리포트 모두 같은 코드 검증(`core/verify.py`)을 통과해야 한다.
- 코드 검증(수치 일치, 인용 유효성)을 먼저 하고, 통과한 것만 Gemini로 판정한다.
- 인프라 실패(모델 서버 5xx, 연결 끊김, `ErrorDeviceLost`, Gemini 429)는 재시도 대상이며 유저 실패 카운터에 넣지 않는다.

## 측정된 제약 (근거 없이 바꾸지 않는다)
- 생성 약 4.5 tok/s, 프롬프트 처리 약 70 tok/s (`qwen3.5:27b`, iGPU)
- 추론 on은 실시간에 쓰지 않는다: 28토큰 질문에 232초, 답변 0자 (1,024토큰 상한 도달)
- iGPU에서 `vk::Queue::submit: ErrorDeviceLost`로 러너가 재시작된 적 있음 (복구 약 15초, 원인 확인 중)
- 질문 300자, 근거 문맥 2,000자, `num_predict` 256, `num_ctx` 4096

## 배포 구조
- ArgoCD가 `master` 브랜치의 `manifests/<앱>`을 자동 동기화한다. `master`에 머지되면 곧바로 클러스터에 반영된다.
- 이미지 태그는 GitHub Actions가 `manifests/`에 커밋해서 갱신한다.
- 비밀값(Redis 비밀번호, Gemini API 키, DB 접속 정보)은 레포에 넣지 않는다.

## 고정 조건
- 하드웨어: Node A(CPU 6C/12T, 32GB, Proxmox: cloudflare / kubes / postgres VM), Node B(iGPU, 8C/16T, 64GB, Ollama LXC)
- 모델: 생성 `qwen3.5:27b`, 임베딩 `qwen3-embedding:0.6b`, 검증 Gemini API
- 구성: Gateway(Spring), Frontend(React), Django(API + 워커), FastAPI(모델 게이트웨이), Redis, PostgreSQL(pgvector, pgBouncer)
