import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from config import get_settings
from ollama import OllamaClient, OllamaStreamError
from router import router

# 기본 로그 레벨(WARNING)에서는 INFO가 안 보이므로 켠다 (스트림 시간 측정 로그용)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with httpx.AsyncClient() as http:
        app.state.ollama = OllamaClient(http, get_settings())
        yield


app = FastAPI(title="Haema Model Gateway", lifespan=lifespan)
app.include_router(router)


# Ollama 쪽 실패(연결 끊김, 5xx, 타임아웃, 잘린 스트림)는 재시도 대상이다.
@app.exception_handler(httpx.HTTPError)
async def ollama_http_error(_: Request, exc: httpx.HTTPError):
    return JSONResponse(status_code=503, content={"detail": f"ollama: {type(exc).__name__}"})


@app.exception_handler(OllamaStreamError)
async def ollama_stream_error(_: Request, exc: OllamaStreamError):
    return JSONResponse(status_code=503, content={"detail": f"ollama: {exc}"})