# FastAPI 모델 게이트웨이 (Ollama 호출만 담당, 상태 없음)
FROM python:3.11-slim

# uv: 로컬과 같은 도구로 uv.lock 기준 의존성 설치
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH"

# 1. 의존성 먼저 (소스가 바뀌어도 이 레이어는 캐시됨)
#    --no-install-project: 프로젝트 자체(uv_build)는 설치하지 않는다.
#    src/ 가 src/ai/ 구조가 아니라 평평해서 패키지 빌드가 맞지 않고, 필요도 없다.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# 2. 소스 복사
COPY . .

EXPOSE 8000

# src/ 를 경로로 직접 지정한다 (main.py 가 `from config import ...` 처럼 평평하게 import 함).
# 루트 main.py(껍데기)는 쓰지 않는다.
# 워커 1개: iGPU 한 장을 공유하므로 프로세스를 늘려도 처리량이 늘지 않는다.
CMD ["uvicorn", "--app-dir", "src", "main:app", "--host", "0.0.0.0", "--port", "8000"]
