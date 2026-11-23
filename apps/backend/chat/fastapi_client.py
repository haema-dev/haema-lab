"""FastAPI 모델 게이트웨이 클라이언트. 계약 원본은 apps/ai/src/router.py (apps/ai/tests/test_api.py 가 고정).

    POST /embed     {"inputs": [str, ...]}  -> {"embeddings": [[float, ...], ...]}
    POST /generate  {"prompt": str}         -> {"text": str, "truncated": bool}

/judge, /fallback은 ai 쪽에 아직 없다(CLAUDE.md에는 계획만 있음). 생기면 여기에 추가한다.
5xx·429·연결 끊김은 InfraError(재시도 대상, 유저 실패 카운터에 넣지 않음), 그 외 4xx와 계약에 안 맞는 응답은 FastAPIError.
"""

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, TypeVar

import httpx
from django.conf import settings

from chat.models import EMBEDDING_DIMENSIONS

T = TypeVar("T")


class FastAPIError(Exception):
    """재시도해도 소용없는 거절(4xx, 잘못된 응답)."""


class InfraError(FastAPIError):
    """모델 서버 5xx, 연결 끊김, ErrorDeviceLost, 429. 재시도한다."""


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

    def generate(self, prompt: str) -> GenerateResult:
        data = self._post("/generate", {"prompt": prompt})
        text, truncated = data.get("text"), data.get("truncated")
        if not isinstance(text, str):
            raise FastAPIError("/generate: text 없음")
        # 없으면 잘림을 알 수 없다. False 로 가정하면 잘린 답이 완결성 검사를 통과하므로 쓰지 않는다.
        # 배포 중 구버전 게이트웨이가 잠깐 응답하는 경우라 인프라 실패로 보고 재시도한다 (배포 순서 무관).
        if not isinstance(truncated, bool):
            raise InfraError("/generate: truncated 없음(구버전 게이트웨이?)")
        return GenerateResult(text=text, truncated=truncated)


def with_infra_retry(call: Callable[[], T], *, delays: Sequence[float] | None = None, sleep=time.sleep) -> T:
    """InfraError만 대기 후 재시도한다. 기본 대기 합 35초: ErrorDeviceLost 복구(약 15초)를 넘긴다."""
    delays = settings.FASTAPI_INFRA_RETRY_DELAYS if delays is None else delays
    for delay in delays:
        try:
            return call()
        except InfraError:
            sleep(delay)
    return call()
