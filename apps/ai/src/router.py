import json
from collections.abc import AsyncIterator

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ollama import OllamaClient, OllamaStreamError

router = APIRouter()


class EmbedRequest(BaseModel):
    inputs: list[str] = Field(min_length=1)


class GenerateRequest(BaseModel):
    prompt: str = Field(min_length=1)


def get_ollama(request: Request) -> OllamaClient:
    return request.app.state.ollama


def _line(obj: dict) -> str:
    return json.dumps(obj, ensure_ascii=False) + "\n"


@router.get("/health")
async def health():
    return {"status": "ok"}


@router.post("/embed")
async def embed(body: EmbedRequest, client: OllamaClient = Depends(get_ollama)):
    return {"embeddings": await client.embed(body.inputs)}


@router.post("/generate")
async def generate(body: GenerateRequest, client: OllamaClient = Depends(get_ollama)):
    """비스트리밍: 전체 응답을 JSON 하나로 돌려준다."""
    return {"text": await client.generate(body.prompt)}


@router.post("/generate/stream")
async def generate_stream(body: GenerateRequest, client: OllamaClient = Depends(get_ollama)):
    """스트리밍: 줄마다 JSON 하나(NDJSON)를 흘려보낸다."""
    gen = client.generate_stream(body.prompt)

    # 첫 조각을 먼저 받아 본다. 연결 실패/모델 에러는 여기서 예외가 나고,
    # main.py의 exception_handler가 503으로 바꿔 준다.
    # (스트림이 시작되면 이미 200이 나간 뒤라 상태 코드를 못 바꾼다)
    try:
        first = await anext(gen)
    except StopAsyncIteration:
        first = None

    async def body_iter() -> AsyncIterator[str]:
        try:
            if first is not None:
                yield _line({"text": first})
            async for piece in gen:
                yield _line({"text": piece})
            yield _line({"done": True})
        except (httpx.HTTPError, OllamaStreamError) as e:
            # 이미 200이 나간 뒤: 마지막 줄에 에러를 실어 보낸다
            yield _line({"error": repr(e)})

    return StreamingResponse(body_iter(), media_type="application/x-ndjson")