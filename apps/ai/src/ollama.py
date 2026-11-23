import json
import logging
import time
from collections.abc import AsyncIterator

import httpx

from config import Settings

log = logging.getLogger(__name__)

# CLAUDE.md 측정된 제약
NUM_PREDICT = 256
NUM_CTX = 4096
SYSTEM = "한국어로 2문장 이내로 답한다."

# embed는 모델 교체/로드 시간이 포함될 수 있어 여유를 둔다 (10초는 추정상 짧음, 미측정)
EMBED_TIMEOUT = 30
# generate는 httpx read timeout 기준: 첫 토큰까지(프롬프트 처리) 또는 토큰 간 최대 대기
GENERATE_TIMEOUT = 180


class OllamaStreamError(RuntimeError):
    """스트림 도중 Ollama가 {"error": ...}를 보낸 경우."""


def _s(ns: object) -> float:
    return ns / 1e9 if isinstance(ns, (int, float)) else 0.0


def _rate(count: object, ns: object) -> float:
    seconds = _s(ns)
    return count / seconds if isinstance(count, (int, float)) and seconds > 0 else 0.0


def log_generate_metrics(data: dict, wall_s: float) -> None:
    """README 의 측정값(생성 tok/s, 프롬프트 처리 tok/s, TTFT, 총 시간)을 요청마다 한 줄로 남긴다.

    값은 Ollama 응답의 *_duration(ns)·*_count 그대로다. prompt_tokens 가 프롬프트 길이보다 훨씬 작으면
    KV 캐시가 재사용된 것이라 prompt_tok_s 가 비현실적으로 커진다 (README: 10,269 tok/s 사례).
    집계: kubectl logs deploy/cloud-ai | grep 'metric=generate'
    """
    log.info(
        "metric=generate prompt_tokens=%s prompt_tok_s=%.1f output_tokens=%s output_tok_s=%.2f "
        "ttft_s=%.2f load_s=%.2f total_s=%.2f wall_s=%.2f done_reason=%s",
        data.get("prompt_eval_count"),
        _rate(data.get("prompt_eval_count"), data.get("prompt_eval_duration")),
        data.get("eval_count"),
        _rate(data.get("eval_count"), data.get("eval_duration")),
        _s(data.get("load_duration")) + _s(data.get("prompt_eval_duration")),
        _s(data.get("load_duration")),
        _s(data.get("total_duration")),
        wall_s,
        data.get("done_reason"),
    )


class OllamaClient:
    def __init__(self, http: httpx.AsyncClient, settings: Settings):
        self._http = http
        self._s = settings
        self._base = settings.ollama_base_url.rstrip("/")

    def _generate_payload(self, prompt: str, stream: bool) -> dict:
        return {
            "model": self._s.generate_model,
            "system": SYSTEM,
            "prompt": prompt,
            "stream": stream,
            "think": False,
            "options": {"num_predict": NUM_PREDICT, "num_ctx": NUM_CTX},
        }

    def _timeout(self, read: float) -> httpx.Timeout:
        # 숫자 하나만 넘기면 connect 에도 같은 값이 걸려, Ollama 에 닿지 않을 때 embed 30초·generate 180초를 기다린다.
        # 연결은 connect_timeout(기본 5초) 안에 실패시키고, 느린 생성은 read 로만 기다린다.
        return httpx.Timeout(read, connect=self._s.connect_timeout)

    async def _post(self, path: str, payload: dict, timeout: float) -> dict:
        resp = await self._http.post(f"{self._base}{path}", json=payload, timeout=self._timeout(timeout))
        resp.raise_for_status()
        return resp.json()

    async def embed(self, inputs: list[str]) -> list[list[float]]:
        t0 = time.perf_counter()
        data = await self._post(
            "/api/embed",
            {"model": self._s.embed_model, "input": inputs},
            timeout=EMBED_TIMEOUT,
        )
        # README: 질의 임베딩 약 0.65초. inputs=1 이 질의, 그 이상은 색인 배치다.
        log.info(
            "metric=embed inputs=%d total_s=%.2f wall_s=%.2f",
            len(inputs), _s(data.get("total_duration")), time.perf_counter() - t0,
        )
        return data["embeddings"]

    async def generate(self, prompt: str) -> tuple[str, bool]:
        """비스트리밍: (답, 잘림 여부). num_predict 상한에 걸리면 Ollama 가 done_reason "length" 를 준다.

        잘린 답은 문장 경계에서 끊길 수 있어 글자만으로는 알 수 없으므로 이 값을 그대로 넘긴다.
        """
        t0 = time.perf_counter()
        data = await self._post(
            "/api/generate",
            self._generate_payload(prompt, stream=False),
            timeout=GENERATE_TIMEOUT,
        )
        log_generate_metrics(data, time.perf_counter() - t0)
        return data["response"], data.get("done_reason") == "length"

    async def generate_stream(self, prompt: str) -> AsyncIterator[str]:
        """스트리밍: Ollama NDJSON을 읽어 토큰 조각(str)을 yield 한다."""
        t0 = time.perf_counter()
        log.info("[stream] 요청 시작")
        first_logged = False
        finished = False
        try:
            async for piece in self._stream_pieces(prompt):
                if not first_logged:
                    first_logged = True
                    log.info("[stream] 첫 응답 %.2fs", time.perf_counter() - t0)
                yield piece
            finished = True
        finally:
            total = time.perf_counter() - t0
            if finished:
                log.info("[stream] 응답 완료 %.2fs", total)
            else:
                log.warning("[stream] 중단/실패 %.2fs (첫 응답 %s)", total,
                            "받음" if first_logged else "못 받음")

    async def _stream_pieces(self, prompt: str) -> AsyncIterator[str]:
        async with self._http.stream(
            "POST",
            f"{self._base}/api/generate",
            json=self._generate_payload(prompt, stream=True),
            timeout=self._timeout(GENERATE_TIMEOUT),
        ) as resp:
            if resp.status_code >= 400:
                await resp.aread()  # 스트림 응답은 본문을 읽어야 에러 내용을 볼 수 있다
                resp.raise_for_status()

            finished = False
            async for line in resp.aiter_lines():
                if not line:
                    continue
                try:
                    chunk = json.loads(line)
                except json.JSONDecodeError as e:
                    raise OllamaStreamError(f"깨진 스트림 줄: {line[:100]!r}") from e
                if "error" in chunk:
                    raise OllamaStreamError(chunk["error"])
                if chunk.get("response"):
                    yield chunk["response"]
                if chunk.get("done"):
                    finished = True
                    break

            if not finished:
                # done 없이 연결이 끝남: 러너 재시작(ErrorDeviceLost 등)으로 잘린 응답
                raise OllamaStreamError("스트림이 done 없이 종료됨")