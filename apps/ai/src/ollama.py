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

    async def _post(self, path: str, payload: dict, timeout: float) -> dict:
        resp = await self._http.post(f"{self._base}{path}", json=payload, timeout=timeout)
        resp.raise_for_status()
        return resp.json()

    async def embed(self, inputs: list[str]) -> list[list[float]]:
        data = await self._post(
            "/api/embed",
            {"model": self._s.embed_model, "input": inputs},
            timeout=EMBED_TIMEOUT,
        )
        return data["embeddings"]

    async def generate(self, prompt: str) -> str:
        """비스트리밍: 전체 응답을 한 번에 받는다."""
        data = await self._post(
            "/api/generate",
            self._generate_payload(prompt, stream=False),
            timeout=GENERATE_TIMEOUT,
        )
        return data["response"]

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
            timeout=GENERATE_TIMEOUT,
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