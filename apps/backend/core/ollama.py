"""Minimal Ollama HTTP client built on the standard library.

Every request has an explicit timeout and there are no automatic retries, so a
slow or unreachable inference node surfaces as ``OllamaError`` immediately.
Infrastructure failures (connection loss, timeouts, HTTP 5xx such as an iGPU
``ErrorDeviceLost`` runner crash) raise ``OllamaUnavailableError`` so callers can
retry them instead of blaming the request.
"""

from __future__ import annotations

import http.client
import json
import os
from dataclasses import dataclass
from typing import IO, Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

# Qwen3-Embedding is trained with an instruction prefix on the *query* side only.
# Documents are embedded as-is. Leaving this out measurably lowers retrieval quality.
DEFAULT_QUERY_INSTRUCTION = (
    "Given a Korean question about economic and monetary policy events, "
    "retrieve official records that answer the question"
)
# Estimate, not measured: the worst-case prompt is ~2.6k characters
# (rag.MAX_CONTEXT_CHARS + question limit + system prompt), which should fit with
# num_predict inside 4096 tokens while halving the KV cache 8192 reserved on the
# shared-memory iGPU. Verify with prompt_eval_count reported in the RAG trailer.
DEFAULT_NUM_CTX = 4096
# Generation runs at ~4.5 tok/s on the iGPU (measured), so output length dominates
# latency: 256 tokens caps a reply at roughly one minute.
DEFAULT_NUM_PREDICT = 256
# Keep the 17 GB chat model resident; Ollama unloads idle models after 5 minutes.
DEFAULT_KEEP_ALIVE = "30m"


class OllamaError(Exception):
    """Raised when the Ollama API cannot serve the request."""


class OllamaConfigurationError(OllamaError):
    """Raised when a required Ollama runtime setting is missing or invalid."""


class OllamaUnavailableError(OllamaError):
    """Raised when the inference node itself failed; the same request may succeed later."""


# Connection-level failures, including a reset or truncated body while reading the
# response (ConnectionResetError, RemoteDisconnected, IncompleteRead). URLError and
# TimeoutError are OSError subclasses.
_TRANSPORT_ERRORS = (OSError, http.client.HTTPException)


def _wrap(exc: Exception, message: str) -> OllamaError:
    """HTTP 4xx means the request was wrong; anything else is the server's fault."""
    if isinstance(exc, HTTPError) and exc.code < 500:
        return OllamaError(message)
    return OllamaUnavailableError(message)


@dataclass(frozen=True)
class OllamaConfig:
    base_url: str
    chat_model: str
    embedding_model: str
    # Ollama silently truncates prompts longer than num_ctx, so it is always sent.
    num_ctx: int = DEFAULT_NUM_CTX
    num_predict: int = DEFAULT_NUM_PREDICT
    keep_alive: str = DEFAULT_KEEP_ALIVE
    think: bool = False
    # Run the embedding model on CPU so it never swaps with the chat model on the iGPU.
    embed_on_cpu: bool = False
    query_instruction: str = DEFAULT_QUERY_INSTRUCTION
    embed_timeout: float = 30.0
    chat_timeout: float = 120.0

    def __post_init__(self) -> None:
        for name in ("base_url", "chat_model", "embedding_model"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise OllamaConfigurationError(f"OLLAMA_{name.upper()} 설정이 필요합니다.")
        if self.num_ctx <= 0:
            raise OllamaConfigurationError("OLLAMA_NUM_CTX는 양수여야 합니다.")
        if self.num_predict <= 0:
            raise OllamaConfigurationError("OLLAMA_NUM_PREDICT는 양수여야 합니다.")

    @classmethod
    def from_env(cls) -> OllamaConfig:
        """Build a config from OS environment variables (used by standalone scripts)."""
        return cls(
            base_url=os.getenv("OLLAMA_BASE_URL", ""),
            chat_model=os.getenv("OLLAMA_CHAT_MODEL", ""),
            embedding_model=os.getenv("OLLAMA_EMBEDDING_MODEL", ""),
            num_ctx=_parse_int(os.getenv("OLLAMA_NUM_CTX"), DEFAULT_NUM_CTX),
            num_predict=_parse_int(os.getenv("OLLAMA_NUM_PREDICT"), DEFAULT_NUM_PREDICT),
            keep_alive=os.getenv("OLLAMA_KEEP_ALIVE") or DEFAULT_KEEP_ALIVE,
            think=_parse_bool(os.getenv("OLLAMA_THINK"), False),
            embed_on_cpu=_parse_bool(os.getenv("OLLAMA_EMBED_ON_CPU"), False),
            query_instruction=os.getenv("OLLAMA_QUERY_INSTRUCTION") or DEFAULT_QUERY_INSTRUCTION,
        )


def _parse_int(raw: str | None, default: int) -> int:
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise OllamaConfigurationError(f"정수 설정값이 아닙니다: {raw!r}") from exc


def _parse_bool(raw: str | None, default: bool) -> bool:
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def format_query(instruction: str, query: str) -> str:
    """Apply the Qwen3-Embedding query template; an empty instruction disables it."""
    if not instruction:
        return query
    return f"Instruct: {instruction}\nQuery: {query}"


class OllamaClient:
    """Thin wrapper over the Ollama REST API endpoints this backend uses."""

    def __init__(self, config: OllamaConfig) -> None:
        self.config = config

    def _url(self, path: str) -> str:
        return f"{self.config.base_url.strip().rstrip('/')}{path}"

    def build_request(self, path: str, payload: dict[str, Any] | None) -> Request:
        if payload is None:
            return Request(self._url(path), method="GET")
        return Request(
            self._url(path),
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )

    def _call_json(self, path: str, payload: dict[str, Any] | None, timeout: float) -> dict[str, Any]:
        try:
            with urlopen(self.build_request(path, payload), timeout=timeout) as response:
                body = json.load(response)
        except _TRANSPORT_ERRORS as exc:
            raise _wrap(exc, f"Ollama API 요청에 실패했습니다: {path}") from exc
        except json.JSONDecodeError as exc:
            raise OllamaError(f"Ollama 응답이 JSON이 아닙니다: {path}") from exc
        if not isinstance(body, dict):
            raise OllamaError(f"Ollama 응답 형식이 올바르지 않습니다: {path}")
        return body

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed texts as-is (document side). Returns one vector per input."""
        if not texts:
            return []
        payload: dict[str, Any] = {"model": self.config.embedding_model, "input": texts}
        if self.config.embed_on_cpu:
            payload["options"] = {"num_gpu": 0}
        body = self._call_json("/api/embed", payload, self.config.embed_timeout)
        embeddings = body.get("embeddings")
        if not isinstance(embeddings, list) or len(embeddings) != len(texts):
            raise OllamaError("Ollama 임베딩 응답 형식이 올바르지 않습니다.")
        return embeddings

    def embed_query(self, query: str) -> list[float]:
        """Embed a search query with the configured instruction prefix."""
        return self.embed([format_query(self.config.query_instruction, query)])[0]

    def chat_payload(self, messages: list[dict[str, str]], stream: bool = True) -> dict[str, Any]:
        return {
            "model": self.config.chat_model,
            "messages": messages,
            "stream": stream,
            "think": self.config.think,
            "keep_alive": self.config.keep_alive,
            "options": {"num_ctx": self.config.num_ctx, "num_predict": self.config.num_predict},
        }

    def open_chat_stream(self, messages: list[dict[str, str]]) -> IO[bytes]:
        """Open the NDJSON chat stream; the caller owns and must close the response."""
        request = self.build_request("/api/chat", self.chat_payload(messages))
        try:
            return urlopen(request, timeout=self.config.chat_timeout)
        except _TRANSPORT_ERRORS as exc:
            raise _wrap(exc, "채팅 스트림을 시작할 수 없습니다.") from exc

    def running_models(self, timeout: float = 10.0) -> list[dict[str, Any]]:
        """Return ``/api/ps`` entries (used to check GPU/CPU offload split)."""
        models = self._call_json("/api/ps", None, timeout).get("models", [])
        return models if isinstance(models, list) else []
