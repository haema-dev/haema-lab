from django.http import HttpResponse

def home():
    return HttpResponse("<h1>Hello World</h1><p>2026년 Django 서버가 정상 작동 중입니다.</p>")
"""HTTP views for the backend."""

from __future__ import annotations

import json

from django.conf import settings
from django.http import HttpRequest, HttpResponse, JsonResponse, StreamingHttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from core.ollama import OllamaConfigurationError, OllamaError, OllamaUnavailableError
from core.rag import (
    DataNotFoundError,
    GenerationBusyError,
    RagError,
    acquire_generation_slot,
    build_messages,
    iter_ndjson,
    open_chat_stream,
    retrieve,
)

# A Korean question JSON-escaped as \uXXXX is 6 bytes per character:
# 300 chars -> 1,800 bytes, plus the JSON envelope.
MAX_BODY_BYTES = 2_048
RETRY_AFTER_SECONDS = "30"
DEFAULT_MAX_QUESTION_CHARS = 300


def home(request: HttpRequest) -> HttpResponse:
    return HttpResponse("<h1>Hello World</h1><p>2026년 Django 서버가 정상 작동 중입니다.</p>")


def _retry_later(message: str, status: int) -> JsonResponse:
    response = JsonResponse({"error": message}, status=status)
    response["Retry-After"] = RETRY_AFTER_SECONDS
    return response


def _error_response(exc: RagError | OllamaError) -> JsonResponse:
    if isinstance(exc, DataNotFoundError):
        return JsonResponse({"error": str(exc)}, status=400)
    if isinstance(exc, OllamaConfigurationError):
        return JsonResponse({"error": "서버의 모델 설정이 올바르지 않습니다."}, status=500)
    if isinstance(exc, OllamaUnavailableError):
        return _retry_later("모델 서버가 일시적으로 응답하지 않습니다.", status=503)
    return JsonResponse({"error": str(exc)}, status=502)


# Stateless JSON API without cookie/session auth, so CSRF tokens protect nothing here.
# Remove this exemption if session authentication is added to this endpoint.
@csrf_exempt
@require_POST
def rag_chat(request: HttpRequest) -> HttpResponse:
    """Answer a question from the monthly JSON data through an Ollama stream.

    The last NDJSON line is a ``{"rag": ...}`` trailer with the verification result.
    """
    if len(request.body) > MAX_BODY_BYTES:
        return JsonResponse({"error": "요청 본문이 너무 큽니다."}, status=413)
    try:
        payload = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "올바른 JSON 요청이 아닙니다."}, status=400)

    question = payload.get("question") if isinstance(payload, dict) else None
    if not isinstance(question, str) or not question.strip():
        return JsonResponse({"error": "question 문자열이 필요합니다."}, status=400)
    question = question.strip()
    max_chars = getattr(settings, "RAG_MAX_QUESTION_CHARS", DEFAULT_MAX_QUESTION_CHARS)
    if len(question) > max_chars:
        return JsonResponse({"error": f"질문은 {max_chars:,}자 이하여야 합니다."}, status=400)

    # Take the slot before retrieval: the query embedding also runs on the iGPU.
    try:
        slot = acquire_generation_slot()
    except GenerationBusyError as exc:
        return _retry_later(str(exc), status=429)
    try:
        result = retrieve(
            question,
            top_k=getattr(settings, "RAG_TOP_K", 4),
            min_similarity=getattr(settings, "RAG_MIN_SIMILARITY", None),
        )
        upstream = open_chat_stream(build_messages(question, result), slot)
    except (OllamaError, RagError) as exc:
        slot.release()
        return _error_response(exc)
    except BaseException:
        slot.release()
        raise

    response = StreamingHttpResponse(
        iter_ndjson(upstream, result.documents),
        content_type="application/x-ndjson; charset=utf-8",
    )
    response["Cache-Control"] = "no-cache"
    response["X-Accel-Buffering"] = "no"
    response["X-RAG-Scope"] = result.scope
    response["X-RAG-Retrieval"] = result.mode
    return response