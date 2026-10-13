"""FastAPI 모델 게이트웨이 클라이언트. 계약은 ai 브랜치 apps/ai/src/router.py 기준.

    POST /embed     {"inputs": [str, ...]}  -> {"embeddings": [[float, ...], ...]}
    POST /generate  {"prompt": str}         -> {"text": str}

/judge, /fallback은 ai 브랜치 router.py에 아직 없다(CLAUDE.md에는 계획만 있음). 생기면 여기에 추가한다.
5xx·연결 끊김은 InfraError(재시도 대상, 유저 실패 카운터에 넣지 않음), 그 외 4xx는 FastAPIError.
"""

import time
from collections.abc import Callable, Sequence
from typing import Any, TypeVar

import httpx
from django.conf import settings

from chat.models import EMBEDDING_DIMENSIONS

T = TypeVar("T")


class FastAPIError(Exception):
    """재시도해도 소용없는 거절(4xx, 잘못된 응답)."""


class InfraError(FastAPIError):
    """모델 서버 5xx, 연결 끊김, ErrorDeviceLost, 429. 재시도한다."""


class FastAPIClient:
    def __init__(self, base_url: str | None = None, *, timeout: float | None = None) -> None:
        self._http = httpx.Client(
            base_url=base_url or settings.FASTAPI_BASE_URL,
            timeout=timeout or settings.FASTAPI_TIMEOUT_SECONDS,
        )

    def close(self) -> None:
        self._http.close()

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            response = self._http.post(path, json=payload)
        except httpx.TransportError as exc:
            raise InfraError(f"{path}: {exc.__class__.__name__}: {exc}") from exc
        if response.status_code == 429 or response.status_code >= 500:
            raise InfraError(f"{path}: HTTP {response.status_code}: {response.text[:200]}")
        if response.status_code >= 400:
            raise FastAPIError(f"{path}: HTTP {response.status_code}: {response.text[:200]}")
        try:
            data = response.json()
        except ValueError as exc:
            raise FastAPIError(f"{path}: JSON이 아닌 응답") from exc
        if not isinstance(data, dict):
            raise FastAPIError(f"{path}: JSON 객체가 아닌 응답")
        return data

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        vectors = self._post("/embed", {"inputs": list(texts)}).get("embeddings")
        if not isinstance(vectors, list) or len(vectors) != len(texts):
            raise FastAPIError("/embed: 입력 수와 임베딩 수가 다르다")
        if any(not isinstance(v, list) or len(v) != EMBEDDING_DIMENSIONS for v in vectors):
            raise FastAPIError(f"/embed: {EMBEDDING_DIMENSIONS}차원이 아니다(다른 임베딩 모델?)")
        return vectors

    def generate(self, prompt: str) -> str:
        text = self._post("/generate", {"prompt": prompt}).get("text")
        if not isinstance(text, str):
            raise FastAPIError("/generate: text 없음")
        return text


def with_infra_retry(call: Callable[[], T], *, delays: Sequence[float] | None = None, sleep=time.sleep) -> T:
    """InfraError만 대기 후 재시도한다. 기본 대기 합 35초: ErrorDeviceLost 복구(약 15초)를 넘긴다."""
    delays = settings.FASTAPI_INFRA_RETRY_DELAYS if delays is None else delays
    for delay in delays:
        try:
            return call()
        except InfraError:
            sleep(delay)
    return call()
