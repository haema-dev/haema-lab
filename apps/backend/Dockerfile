# 1. uv가 포함된 파이썬 3.13 이미지 (3.13.5 포함 안정 버전)
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim

# 2. 환경 변수 설정 (파이썬 바이트코드 미생성 및 버퍼링 방지)
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# 3. 작업 디렉토리 설정
WORKDIR /app

# 4. 의존성 파일 복사 (캐싱 최적화)
COPY pyproject.toml uv.lock ./

# 5. 의존성 설치
# --frozen: uv.lock과 일치하지 않으면 에러 발생 (버전 강제 고정)
# --no-install-project: 앱 자체는 나중에 설치
RUN uv sync --frozen --no-cache --no-install-project

# 6. 소스 코드 복사
COPY . .

# 7. 실행 (Gunicorn + Uvicorn Worker로 비동기/동기 모두 대응)
# 2026년 기준, Django도 ASGI를 적극 활용하므로 uvicorn 워커를 섞는 것이 효율적입니다.
CMD ["uv", "run", "gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "4", "--worker-class", "uvicorn.workers.UvicornWorker"]