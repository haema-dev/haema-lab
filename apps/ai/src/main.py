import logging
import time
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from config import get_settings
from ollama import OllamaClient, OllamaStreamError
from router import router

# 기본 로그 레벨(WARNING)에서는 INFO가 안 보이므로 켠다 (요청/응답 한 줄, 측정 로그용)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# 요청 한 줄 + 응답 한 줄. Django 가 보낸 X-Request-Id 로 양쪽 로그를 맞춰 볼 수 있다.
access = logging.getLogger("access")


class _SkipHealthAccessLog(logging.Filter):
    """K8s 프로브가 /health 를 10~30초마다 찔러 access 로그가 묻히므로 그 줄만 뺀다. 실제 요청은 그대로 찍는다."""

    def filter(self, record: logging.LogRecord) -> bool:
        # uvicorn.access 의 args: (client_addr, method, path, http_version, status_code)
        args = record.args
        return not (isinstance(args, tuple) and len(args) >= 3 and args[2] == "/health")


uvicorn_access = logging.getLogger("uvicorn.access")
uvicorn_access.addFilter(_SkipHealthAccessLog())
# 아래 log_requests 미들웨어가 요청·응답을 찍으므로 uvicorn 의 같은 내용 줄은 끈다(중복).
uvicorn_access.setLevel(logging.WARNING)


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with httpx.AsyncClient() as http:
        app.state.ollama = OllamaClient(http, get_settings())
        yield


app = FastAPI(title="Haema Model Gateway", lifespan=lifespan)
app.include_router(router)


@app.middleware("http")
async def log_requests(request: Request, call_next):
    if request.url.path == "/health":
        return await call_next(request)
    rid = request.headers.get("x-request-id", "-")
    method, path = request.method, request.url.path
    access.info("req id=%s %s %s", rid, method, path)
    t0 = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        access.exception("res id=%s %s %s status=500 code=unhandled dur=%.2fs", rid, method, path,
                         time.perf_counter() - t0)
        raise
    status = response.status_code
    code = getattr(request.state, "error_code", None) or ("ok" if status < 400 else f"http_{status}")
    # 스트리밍(/generate/stream)은 헤더가 나간 시점까지의 시간이다.
    access.log(logging.INFO if status < 400 else logging.WARNING,
               "res id=%s %s %s status=%s code=%s dur=%.2fs", rid, method, path, status, code,
               time.perf_counter() - t0)
    return response


def classify_ollama_error(exc: Exception) -> tuple[int, str]:
    """Ollama 쪽 실패를 (응답 상태코드, 에러 코드)로 나눈다. 전부 5xx 라 Django 는 재시도 대상(인프라 실패)으로 본다.

    503 ollama_unreachable   연결 자체가 안 됨 (주소·네트워크·Ollama 꺼짐)
    504 ollama_timeout       연결은 됐는데 응답이 read 타임아웃 안에 안 옴 (모델 로드·생성 지연)
    502 ollama_http_<코드>   Ollama 가 에러 상태로 응답 (404 = 모델 없음, 500 = 러너 에러 등)
    502 ollama_disconnected  응답 도중 연결이 끊김 (러너 재시작, ErrorDeviceLost 등)
    502 ollama_stream_broken 스트림이 깨지거나 done 없이 끝남, 또는 스트림 안의 {"error": ...}
    503 ollama_unavailable   그 밖의 httpx 실패
    """
    if isinstance(exc, OllamaStreamError):
        return 502, "ollama_stream_broken"
    if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)):
        return 503, "ollama_unreachable"
    if isinstance(exc, httpx.TimeoutException):
        return 504, "ollama_timeout"
    if isinstance(exc, httpx.HTTPStatusError):
        return 502, f"ollama_http_{exc.response.status_code}"
    if isinstance(exc, (httpx.ReadError, httpx.WriteError, httpx.RemoteProtocolError)):
        return 502, "ollama_disconnected"
    return 503, "ollama_unavailable"


def _ollama_error_response(request: Request, exc: Exception) -> JSONResponse:
    status, code = classify_ollama_error(exc)
    request.state.error_code = code
    detail = str(exc) if isinstance(exc, OllamaStreamError) else type(exc).__name__
    return JSONResponse(status_code=status, content={"detail": f"ollama: {detail}", "code": code})


# Ollama 쪽 실패(연결 끊김, 5xx, 타임아웃, 잘린 스트림)는 재시도 대상이다.
@app.exception_handler(httpx.HTTPError)
async def ollama_http_error(request: Request, exc: httpx.HTTPError):
    return _ollama_error_response(request, exc)


@app.exception_handler(OllamaStreamError)
async def ollama_stream_error(request: Request, exc: OllamaStreamError):
    return _ollama_error_response(request, exc)
