FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# 의존성만 먼저 복사해 소스가 바뀌어도 이 레이어는 캐시를 쓴다.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

# API 서버. 워커(manage.py run_worker)는 같은 이미지에서 command 로 덮어쓴다.
# Deployment 의 containerPort, Service 의 targetPort 와 같은 8000 이어야 한다.
CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "2"]
