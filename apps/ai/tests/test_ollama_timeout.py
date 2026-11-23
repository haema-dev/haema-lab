"""Ollama 호출 타임아웃: 연결은 connect_timeout 안에 실패하고, read 만 embed/generate 값을 쓴다.

실제 네트워크 없이 httpx.MockTransport 로 요청에 실린 timeout 값만 확인한다.
"""

import asyncio

import httpx

from config import Settings
from ollama import EMBED_TIMEOUT, GENERATE_TIMEOUT, OllamaClient


def _settings() -> Settings:
    return Settings(ollama_base_url="http://ollama.test:11434", generate_model="g", embed_model="e")


def _run(call):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.extensions["timeout"])
        if request.url.path == "/api/embed":
            return httpx.Response(200, json={"embeddings": [[0.1]]})
        return httpx.Response(200, json={"response": "ok", "done_reason": "stop", "done": True})

    async def main():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            return await call(OllamaClient(http, _settings()))

    asyncio.run(main())
    return seen[0]


def test_embed_connect_timeout_is_short_and_read_keeps_embed_timeout():
    timeout = _run(lambda c: c.embed(["q"]))
    assert timeout["connect"] == _settings().connect_timeout == 5.0
    assert timeout["read"] == EMBED_TIMEOUT


def test_generate_connect_timeout_is_short_and_read_keeps_generate_timeout():
    timeout = _run(lambda c: c.generate("p"))
    assert timeout["connect"] == 5.0
    assert timeout["read"] == GENERATE_TIMEOUT


def test_unreachable_ollama_fails_as_connect_error_not_after_read_timeout():
    def handler(request):
        raise httpx.ConnectTimeout("unreachable", request=request)

    async def main():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            await OllamaClient(http, _settings()).embed(["q"])

    try:
        asyncio.run(main())
    except httpx.ConnectTimeout:
        pass
    else:
        raise AssertionError("ConnectTimeout 이 그대로 올라와야 router 가 503 으로 바꾼다")
