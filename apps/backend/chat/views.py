"""대화·메시지 CRUD. 유저 id는 Gateway가 인증 후 넣는 X-User-Id 헤더에서 받는다(Django는 Gateway 뒤에서만 열림).

메시지 생성(POST)만 모델이 필요하므로 Redis 큐에 message_id를 한 번 넣는다. 나머지는 DB 조회다.
원본 문서 색인(source_*)과 답변 캐시(answer_cache)는 배치/관리자(admin)에서만 다룬다.
"""

import json
import logging

import redis
from django.conf import settings
from django.db import transaction
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from chat.models import Conversation, Message
from chat.redis_queue import enqueue_message

logger = logging.getLogger(__name__)

# 한글을 \uXXXX로 이스케이프하면 글자당 6바이트: 300자 ≈ 1,800바이트 + 봉투.
MAX_BODY_BYTES = 2_048


def _err(message: str, status: int) -> JsonResponse:
    return JsonResponse({"error": message}, status=status)


def _user_id(request: HttpRequest) -> int | None:
    raw = request.META.get("HTTP_X_USER_ID", "")
    return int(raw) if raw.isdigit() else None


def _body(request: HttpRequest) -> dict | None:
    if len(request.body) > MAX_BODY_BYTES:
        return None
    try:
        data = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _conversation_dict(c: Conversation) -> dict:
    return {"id": c.id, "title": c.title, "created_at": c.created_at, "updated_at": c.updated_at}


def _message_dict(m: Message) -> dict:
    return {
        "id": m.id,
        "conversation_id": m.conversation_id,
        "question": m.question,
        "answer": m.answer,
        "status": m.status,
        "source": m.source,
        "cited_chunk_ids": m.cited_chunk_ids or [],
        "verification": m.verification,
        "created_at": m.created_at,
        "updated_at": m.updated_at,
    }


def _own_conversation(user_id: int, pk: int) -> Conversation | None:
    return Conversation.objects.filter(pk=pk, user_id=user_id).first()


# --- 대화 -------------------------------------------------------------------

@csrf_exempt  # Gateway 뒤의 쿠키 없는 JSON API라 CSRF 토큰이 지켜 줄 것이 없다.
@require_http_methods(["GET", "POST"])
def conversations(request: HttpRequest) -> JsonResponse:
    user_id = _user_id(request)
    if user_id is None:
        return _err("인증 정보가 없습니다.", 401)

    if request.method == "GET":
        rows = Conversation.objects.filter(user_id=user_id).order_by("-updated_at")[:100]
        return JsonResponse({"results": [_conversation_dict(c) for c in rows]})

    data = _body(request)
    if data is None:
        return _err("올바른 JSON 요청이 아닙니다.", 400)
    title = data.get("title", "")
    if not isinstance(title, str) or len(title) > 200:
        return _err("title은 200자 이하 문자열이어야 합니다.", 400)
    conversation = Conversation.objects.create(user_id=user_id, title=title.strip())
    return JsonResponse(_conversation_dict(conversation), status=201)


@csrf_exempt
@require_http_methods(["GET", "PATCH", "DELETE"])
def conversation_detail(request: HttpRequest, conversation_id: int) -> HttpResponse:
    user_id = _user_id(request)
    if user_id is None:
        return _err("인증 정보가 없습니다.", 401)
    conversation = _own_conversation(user_id, conversation_id)
    if conversation is None:
        return _err("대화를 찾을 수 없습니다.", 404)

    if request.method == "DELETE":
        conversation.delete()  # 메시지는 DB의 ON DELETE CASCADE가 지운다.
        return HttpResponse(status=204)

    if request.method == "PATCH":
        data = _body(request)
        title = data.get("title") if data else None
        if not isinstance(title, str) or len(title) > 200:
            return _err("title은 200자 이하 문자열이어야 합니다.", 400)
        conversation.title = title.strip()
        conversation.save(update_fields=["title", "updated_at"])  # auto_now가 updated_at을 갱신한다
    return JsonResponse(_conversation_dict(conversation))


# --- 메시지 -----------------------------------------------------------------

@csrf_exempt
@require_http_methods(["GET", "POST"])
def messages(request: HttpRequest, conversation_id: int) -> JsonResponse:
    user_id = _user_id(request)
    if user_id is None:
        return _err("인증 정보가 없습니다.", 401)
    conversation = _own_conversation(user_id, conversation_id)
    if conversation is None:
        return _err("대화를 찾을 수 없습니다.", 404)

    if request.method == "GET":
        rows = conversation.messages.order_by("created_at", "id")
        return JsonResponse({"results": [_message_dict(m) for m in rows]})

    # 검증을 먼저: DB 쓰기·큐 push·FastAPI 호출 전에 거절한다.
    if len(request.body) > MAX_BODY_BYTES:
        return _err("요청 본문이 너무 큽니다.", 413)
    data = _body(request)
    question = data.get("question") if data else None
    if not isinstance(question, str) or not question.strip():
        return _err("question 문자열이 필요합니다.", 400)
    question = question.strip()
    if len(question) > settings.MAX_QUESTION_CHARS:
        return _err(f"질문은 {settings.MAX_QUESTION_CHARS:,}자 이하여야 합니다.", 400)

    with transaction.atomic():
        message = Message.objects.create(conversation=conversation, user_id=user_id, question=question)
        conversation.save(update_fields=["updated_at"])

    # 커밋 뒤 큐에 한 번만 넣는다(워커가 행을 읽을 수 있도록).
    try:
        enqueue_message(message.id)
    except redis.RedisError:
        logger.exception("enqueue failed for message %s", message.id)
        # 인프라 실패: 유저 탓이 아니므로 failed로 닫고 재시도를 안내한다.
        Message.objects.filter(pk=message.pk).update(
            status=Message.Status.FAILED, error="enqueue failed", updated_at=timezone.now()
        )
        response = _err("잠시 후 다시 시도해 주세요.", 503)
        response["Retry-After"] = "5"
        return response
    return JsonResponse(_message_dict(message), status=202)


@csrf_exempt
@require_http_methods(["GET", "DELETE"])
def message_detail(request: HttpRequest, message_id: int) -> HttpResponse:
    """GET은 폴링용: 큐도 모델도 거치지 않는 DB 조회 한 번."""
    user_id = _user_id(request)
    if user_id is None:
        return _err("인증 정보가 없습니다.", 401)
    message = Message.objects.filter(pk=message_id, conversation__user_id=user_id).first()
    if message is None:
        return _err("메시지를 찾을 수 없습니다.", 404)
    if request.method == "DELETE":
        message.delete()
        return HttpResponse(status=204)
    return JsonResponse(_message_dict(message))
