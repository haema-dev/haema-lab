"""HTTP client for the FastAPI model gateway.

FastAPI holds no DB credentials and stores nothing: Django sends the question
and the evidence, FastAPI calls Ollama or Gemini and returns the result.
Prompts are built on the FastAPI side.

Request/response contract (apps/ai src/ai/schemas.py is the source of truth):

    POST /embed     {"kind": "query"|"document", "inputs": [str, ...]}
                 -> {"model": str, "embeddings": [[float, ...], ...], "dimensions": int}
    POST /generate  {"question": str, "contexts": [{"id": str, "text": str}, ...]}
                 -> {"model": str, "text": str, "done_reason": str|null, "truncated": bool}
    POST /judge     {"question": str, "answer": str, "contexts": [...]}
                 -> {"model": str, "unsupported_claims": [str, ...]}
    POST /fallback  {"question": str, "contexts": [...]}
                 -> {"model": str, "text": str, "finish_reason": str|null, "truncated": bool}

``truncated`` is true when the answer hit the output cap (Ollama done_reason
"length", Gemini finishReason "MAX_TOKENS"). ``contexts`` must not be empty.

Errors come back as {"error": {"kind", "reason", "retryable", ...}}. Only
``retryable: true`` (503: model server 5xx, connection loss, ErrorDeviceLost,
Gemini 429) is an infra failure worth retrying; 502 bad_output /
upstream_rejected and 503 not_configured are not.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, TypeVar

import httpx
from django.conf import settings

from qa.models import EMBEDDING_DIMENSIONS

T = TypeVar("T")

# Ollama logs this when the iGPU runner dies; the runner comes back in ~15s.
DEVICE_LOST_MARKER = "ErrorDeviceLost"


class FastAPIError(Exception):
    """A request the gateway rejected for a reason a retry will not fix (4xx, bad payload)."""


class InfraError(FastAPIError):
    """Model server 5xx, connection loss, ErrorDeviceLost, Gemini 429.

    Retried, and never counted as the user's failure.
    """

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after  # seconds, from the Retry-After header (Gemini 429)


@dataclass(frozen=True)
class Context:
    id: str  # document_chunk.citation_key
    text: str

    def as_dict(self) -> dict[str, str]:
        return {"id": self.id, "text": self.text}


@dataclass(frozen=True)
class Embeddings:
    model: str
    vectors: list[list[float]]


@dataclass(frozen=True)
class Generation:
    model: str
    text: str
    done_reason: str  # Ollama done_reason or Gemini finishReason, for the record
    truncated: bool


@dataclass(frozen=True)
class Judgement:
    model: str
    unsupported_claims: list[str]


class FastAPIClient:
    def __init__(
        self,
        base_url: str | None = None,
        *,
        timeout: float | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._http = httpx.Client(
            base_url=base_url or settings.FASTAPI_BASE_URL,
            # Generation is ~4.5 tok/s: 256 tokens + prompt is about a minute on the iGPU.
            timeout=timeout or settings.FASTAPI_TIMEOUT_SECONDS,
            transport=transport,
        )

    def close(self) -> None:
        self._http.close()

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            response = self._http.post(path, json=payload)
        except httpx.TransportError as exc:  # connect error, read timeout, connection reset
            raise InfraError(f"{path}: {exc.__class__.__name__}: {exc}") from exc

        body = response.text
        status = response.status_code
        if DEVICE_LOST_MARKER in body:
            raise InfraError(f"{path}: {DEVICE_LOST_MARKER}")
        if status >= 400:
            message = f"{path}: HTTP {status}: {body[:200]}"
            retryable = _retryable_flag(response)
            if retryable is None:  # no gateway error body (proxy, crash): judge by status
                retryable = status == 429 or status >= 500
            if retryable:
                raise InfraError(message, retry_after=_retry_after(response))
            raise FastAPIError(message)
        try:
            data = response.json()
        except ValueError as exc:
            raise FastAPIError(f"{path}: response is not JSON") from exc
        if not isinstance(data, dict):
            raise FastAPIError(f"{path}: response is not a JSON object")
        return data

    def embed(self, texts: Sequence[str], *, kind: str) -> Embeddings:
        if kind not in {"query", "document"}:
            raise ValueError(f"unknown embed kind: {kind}")
        data = self._post("/embed", {"kind": kind, "inputs": list(texts)})
        vectors = data.get("embeddings")
        if not isinstance(vectors, list) or len(vectors) != len(texts):
            raise FastAPIError("/embed: embeddings count does not match inputs")
        if any(not isinstance(v, list) or len(v) != EMBEDDING_DIMENSIONS for v in vectors):
            # A different embedding model: the vector(1024) columns would reject it.
            raise FastAPIError(f"/embed: expected {EMBEDDING_DIMENSIONS}-dimension vectors")
        return Embeddings(model=str(data.get("model", "")), vectors=vectors)

    def _generation(self, path: str, payload: dict[str, Any]) -> Generation:
        data = self._post(path, payload)
        text = data.get("text")
        if not isinstance(text, str):
            raise FastAPIError(f"{path}: missing text")
        truncated = data.get("truncated")
        if not isinstance(truncated, bool):
            raise FastAPIError(f"{path}: missing truncated")
        # /generate says done_reason (Ollama), /fallback says finish_reason (Gemini).
        reason = data.get("done_reason") or data.get("finish_reason") or ""
        return Generation(
            model=str(data.get("model", "")), text=text, done_reason=str(reason), truncated=truncated
        )

    def generate(self, question: str, contexts: Sequence[Context]) -> Generation:
        return self._generation(
            "/generate", {"question": question, "contexts": [c.as_dict() for c in contexts]}
        )

    def fallback(self, question: str, contexts: Sequence[Context]) -> Generation:
        return self._generation(
            "/fallback", {"question": question, "contexts": [c.as_dict() for c in contexts]}
        )

    def judge(self, question: str, answer: str, contexts: Sequence[Context]) -> Judgement:
        data = self._post(
            "/judge",
            {"question": question, "answer": answer, "contexts": [c.as_dict() for c in contexts]},
        )
        claims = data.get("unsupported_claims")
        if not isinstance(claims, list):
            raise FastAPIError("/judge: missing unsupported_claims")
        return Judgement(model=str(data.get("model", "")), unsupported_claims=[str(c) for c in claims])


def _retryable_flag(response: httpx.Response) -> bool | None:
    """The gateway's own verdict: {"error": {"retryable": bool}}. None if absent."""
    try:
        error = response.json().get("error")
    except (ValueError, AttributeError):
        return None
    flag = error.get("retryable") if isinstance(error, dict) else None
    return flag if isinstance(flag, bool) else None


def _retry_after(response: httpx.Response) -> float | None:
    try:
        return max(0.0, float(response.headers["retry-after"]))
    except (KeyError, ValueError):
        return None


@dataclass
class RetryCounter:
    count: int = 0


def with_infra_retry(
    call: Callable[[], T],
    *,
    counter: RetryCounter | None = None,
    delays: Sequence[float] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    """Run ``call``; on InfraError wait and retry. Other errors propagate at once.

    Default delays sum to 35s so one ErrorDeviceLost (~15s recovery) fits.
    A Retry-After from the upstream (Gemini 429) stretches that attempt's wait.
    """
    delays = settings.FASTAPI_INFRA_RETRY_DELAYS if delays is None else delays
    for delay in delays:
        try:
            return call()
        except InfraError as exc:
            if counter is not None:
                counter.count += 1
            sleep(max(delay, exc.retry_after or 0.0))
    return call()
