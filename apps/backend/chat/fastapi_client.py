"""FastAPI 모델 게이트웨이 클라이언트. 계약 원본은 apps/ai/src/router.py (apps/ai/tests/test_api.py 가 고정).

    POST /embed     {"inputs": [str, ...]}  -> {"embeddings": [[float, ...], ...]}
    POST /generate  {"prompt": str}         -> {"text": str, "truncated": bool}

/judge, /fallback은 ai 쪽에 아직 없다(CLAUDE.md에는 계획만 있음). 생기면 여기에 추가한다.
5xx·429·연결 끊김은 InfraError(재시도 대상, 유저 실패 카운터에 넣지 않음), 그 외 4xx와 계약에 안 맞는 응답은 FastAPIError.
"""

import logging
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, TypeVar

import httpx
from django.conf import settings

from chat.models import EMBEDDING_DIMENSIONS

log = logging.getLogger(__name__)
T = TypeVar("T")


class FastAPIError(Exception):
    """재시도해도 소용없는 거절(4xx, 잘못된 응답).

    code: 로그·message.error 에 남기는 분류. http_<상태코드>, bad_response 등.
    status: 게이트웨이 HTTP 상태코드(응답을 못 받았으면 None).
    """

    def __init__(self, message: str, *, code: str = "rejected", status: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


class InfraError(FastAPIError):
    """모델 서버 5xx, 연결 끊김, ErrorDeviceLost, 429. 재시도한다.

    code 예:
      ai 까지 못 감   ai_connect_timeout, ai_connect_error, ai_timeout, ai_disconnected
      ai 가 알려준 것  ollama_unreachable(503), ollama_timeout(504), ollama_http_<코드>(502),
                      ollama_disconnected(502), ollama_stream_broken(502)
      그 밖          http_429, http_<5xx>, old_gateway
    """

    def __init__(self, message: str, *, code: str = "infra", status: int | None = None) -> None:
        super().__init__(message, code=code, status=status)


def _transport_code(exc: httpx.TransportError) -> str:
    """Django -> ai 구간에서 응답을 못 받은 경우."""
    if isinstance(exc, httpx.ConnectTimeout):
        return "ai_connect_timeout"
    if isinstance(exc, httpx.ConnectError):
        return "ai_connect_error"
    if isinstance(exc, httpx.TimeoutException):
        return "ai_timeout"
    if isinstance(exc, (httpx.ReadError, httpx.WriteError, httpx.RemoteProtocolError)):
        return "ai_disconnected"
    return "ai_transport_error"


def _body_code(response: httpx.Response) -> str | None:
    """ai 가 실어 보낸 에러 코드({"code": "ollama_unreachable", ...}). 없거나 JSON 이 아니면 None."""
    try:
        data = response.json()
    except ValueError:
        return None
    code = data.get("code") if isinstance(data, dict) else None
    return code if isinstance(code, str) and code else None


@dataclass(frozen=True)
class GenerateResult:
    text: str
    truncated: bool  # num_predict 상한에 걸려 잘림. 완결성 검사(core.verify.check_generation)가 쓴다.


class FastAPIClient:
    def __init__(
        self,
        base_url: str | None = None,
        *,
        timeout: float | None = None,
        transport: httpx.BaseTransport | None = None,  # 테스트에서 httpx.MockTransport 를 넣는다
    ) -> None:
        self._http = httpx.Client(
            base_url=base_url or settings.FASTAPI_BASE_URL,
            timeout=timeout or settings.FASTAPI_TIMEOUT_SECONDS,
            transport=transport,
        )

    def close(self) -> None:
        self._http.close()

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        # 요청 한 줄 + 결과 한 줄. 같은 id 가 ai 쪽 로그(req/res id=...)에도 찍힌다.
        rid = uuid.uuid4().hex[:12]
        log.info("fastapi req id=%s POST %s", rid, path)
        t0 = time.monotonic()

        def result(status: object, code: str, level: int = logging.INFO) -> None:
            log.log(level, "fastapi res id=%s POST %s status=%s code=%s dur=%.2fs",
                    rid, path, status, code, time.monotonic() - t0)

        try:
            response = self._http.post(path, json=payload, headers={"X-Request-Id": rid})
        except httpx.TransportError as exc:
            code = _transport_code(exc)
            result("-", code, logging.WARNING)
            raise InfraError(f"{path}: {code}: {exc.__class__.__name__}: {exc}", code=code) from exc

        status = response.status_code
        if status == 429 or status >= 500:
            code = _body_code(response) or f"http_{status}"
            result(status, code, logging.WARNING)
            raise InfraError(f"{path}: {code} (HTTP {status}): {response.text[:200]}", code=code, status=status)
        if status >= 400:
            code = f"http_{status}"
            result(status, code, logging.WARNING)
            raise FastAPIError(f"{path}: {code}: {response.text[:200]}", code=code, status=status)
        try:
            data = response.json()
        except ValueError as exc:
            result(status, "bad_response", logging.WARNING)
            raise FastAPIError(f"{path}: JSON이 아닌 응답", code="bad_response", status=status) from exc
        if not isinstance(data, dict):
            result(status, "bad_response", logging.WARNING)
            raise FastAPIError(f"{path}: JSON 객체가 아닌 응답", code="bad_response", status=status)
        result(status, "ok")
        return data

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        vectors = self._post("/embed", {"inputs": list(texts)}).get("embeddings")
        if not isinstance(vectors, list) or len(vectors) != len(texts):
            raise FastAPIError("/embed: 입력 수와 임베딩 수가 다르다", code="bad_response")
        if any(not isinstance(v, list) or len(v) != EMBEDDING_DIMENSIONS for v in vectors):
            raise FastAPIError(f"/embed: {EMBEDDING_DIMENSIONS}차원이 아니다(다른 임베딩 모델?)", code="bad_response")
        return vectors

    def generate(self, prompt: str) -> GenerateResult:
        data = self._post("/generate", {"prompt": prompt})
        text, truncated = data.get("text"), data.get("truncated")
        if not isinstance(text, str):
            raise FastAPIError("/generate: text 없음", code="bad_response")
        # 없으면 잘림을 알 수 없다. False 로 가정하면 잘린 답이 완결성 검사를 통과하므로 쓰지 않는다.
        # 배포 중 구버전 게이트웨이가 잠깐 응답하는 경우라 인프라 실패로 보고 재시도한다 (배포 순서 무관).
        if not isinstance(truncated, bool):
            raise InfraError("/generate: truncated 없음(구버전 게이트웨이?)", code="old_gateway")
        return GenerateResult(text=text, truncated=truncated)


def with_infra_retry(call: Callable[[], T], *, delays: Sequence[float] | None = None, sleep=time.sleep) -> T:
    """InfraError만 대기 후 재시도한다. 기본 대기 합 17초: ErrorDeviceLost 복구(약 15초)를 넘긴다."""
    delays = settings.FASTAPI_INFRA_RETRY_DELAYS if delays is None else delays
    for delay in delays:
        try:
            return call()
        except InfraError:
            sleep(delay)
    return call()
