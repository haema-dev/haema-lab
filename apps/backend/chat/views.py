"""대화·메시지 CRUD. 유저 id는 Gateway가 인증 후 넣는 X-User-Id 헤더에서 받는다(Django는 Gateway 뒤에서만 열림).

메시지 생성(POST)만 모델이 필요하므로 Redis 큐에 message_id를 한 번 넣는다. 나머지는 DB 조회다.
원본 문서 색인(source_*)과 답변 캐시(answer_cache)는 배치/관리자(admin)에서만 다룬다.
"""

import json
import logging

import redis
from django.conf import settings
from django.db import IntegrityError, connection, transaction
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from chat.models import Conversation, Message
from chat.redis_queue import enqueue_message

logger = logging.getLogger(__name__)

# 한글을 \uXXXX로 이스케이프하면 글자당 6바이트: 300자 ≈ 1,800바이트 + 봉투.
# 서로게이트 쌍(이모지 등)은 글자당 12바이트라 300자 이내여도 넘을 수 있다.
MAX_BODY_BYTES = 2_048
MAX_TITLE_CHARS = 200

# 메시지 목록 페이지 크기.
DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 100

# 유저당 동시에 처리 대기·처리 중인 메시지 상한. 측정값이 아니라 정책값이다(GPU 처리량이 작아 큐 독점을 막는 용도).
DEFAULT_MAX_PENDING_PER_USER = 3


def _err(message: str, status: int) -> JsonResponse:
    return JsonResponse({"error": message}, status=status)


def _user_id(request: HttpRequest) -> int | None:
    raw = request.META.get("HTTP_X_USER_ID", "")
    # str.isdigit()만 쓰면 "²" 같은 문자가 통과해 int()에서 ValueError가 난다.
    return int(raw) if raw.isascii() and raw.isdigit() else None


def _body(request: HttpRequest) -> tuple[dict | None, JsonResponse | None]:
    """(본문, 오류 응답). 오류가 있으면 본문은 None이다."""
    if len(request.body) > MAX_BODY_BYTES:
        return None, _err("요청 본문이 너무 큽니다.", 413)
    try:
        data = json.loads(request.body or b"{}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None, _err("올바른 JSON 요청이 아닙니다.", 400)
    if not isinstance(data, dict):
        return None, _err("올바른 JSON 요청이 아닙니다.", 400)
    return data, None


def _clean_text(value: object, max_chars: int) -> str | None:
    """문자열이고, NUL이 없고, 공백 제거 후 max_chars 이하면 정리한 값을, 아니면 None을 돌려준다.

    PostgreSQL text 컬럼은 NUL(\\x00)을 거부해 저장 시 500이 되므로 여기서 막는다.
    """
    if not isinstance(value, str) or "\x00" in value:
        return None
    value = value.strip()
    return value if len(value) <= max_chars else None


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


def _int_param(request: HttpRequest, name: str) -> int | None:
    raw = request.GET.get(name, "")
    return int(raw) if raw.isascii() and raw.isdigit() else None


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

    data, error = _body(request)
    if error is not None:
        return error
    title = _clean_text(data.get("title", ""), MAX_TITLE_CHARS)
    if title is None:
        return _err(f"title은 {MAX_TITLE_CHARS}자 이하 문자열이어야 합니다.", 400)
    conversation = Conversation.objects.create(user_id=user_id, title=title)
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
        conversation.delete()  # 메시지 삭제는 Message.conversation FK의 on_delete 설정을 따른다.
        return HttpResponse(status=204)

    if request.method == "PATCH":
        data, error = _body(request)
        if error is not None:
            return error
        title = _clean_text(data.get("title"), MAX_TITLE_CHARS)
        if title is None:
            return _err(f"title은 {MAX_TITLE_CHARS}자 이하 문자열이어야 합니다.", 400)
        conversation.title = title
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
        return _message_page(request, conversation)

    # 검증을 먼저: DB 쓰기·큐 push·FastAPI 호출 전에 거절한다.
    data, error = _body(request)
    if error is not None:
        return error
    question = _clean_text(data.get("question"), settings.MAX_QUESTION_CHARS)
    if not question:
        return _err(f"question은 1자 이상 {settings.MAX_QUESTION_CHARS:,}자 이하 문자열이어야 합니다.", 400)

    # 모델 처리량이 작으므로 유저 한 명이 큐를 채우지 못하게 한다. 동시 요청 사이의 경쟁은 막지 않는다(상한이 정확히 지켜지지는 않는다).
    max_pending = getattr(settings, "MAX_PENDING_PER_USER", DEFAULT_MAX_PENDING_PER_USER)
    in_flight = Message.objects.filter(
        user_id=user_id, status__in=[Message.Status.PENDING, Message.Status.PROCESSING]
    ).count()
    if in_flight >= max_pending:
        response = _err("처리 중인 질문이 너무 많습니다. 답변이 끝난 뒤 다시 질문해 주세요.", 429)
        response["Retry-After"] = "30"
        return response

    try:
        with transaction.atomic():
            message = Message.objects.create(conversation=conversation, user_id=user_id, question=question)
            conversation.save(update_fields=["updated_at"])
    except IntegrityError:
        # 확인 직후 대화가 삭제된 경우(FK 위반).
        return _err("대화를 찾을 수 없습니다.", 404)

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


def _message_page(request: HttpRequest, conversation: Conversation) -> JsonResponse:
    """최신 limit개를 오래된 순으로 돌려준다. before_id를 주면 그 id보다 오래된 메시지를 이어서 준다."""
    limit = _int_param(request, "limit") or DEFAULT_PAGE_SIZE
    limit = min(limit, MAX_PAGE_SIZE)
    qs = conversation.messages.order_by("-id")
    before_id = _int_param(request, "before_id")
    if before_id is not None:
        qs = qs.filter(id__lt=before_id)

    rows = list(qs[: limit + 1])  # 한 건 더 읽어서 이어질 페이지가 있는지 본다.
    has_more = len(rows) > limit
    rows = rows[:limit][::-1]
    return JsonResponse(
        {
            "results": [_message_dict(m) for m in rows],
            "has_more": has_more,
            "next_before_id": rows[0].id if has_more and rows else None,
        }
    )


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


# --- 테스트 api (임시) ---------------------------------------------------------
# public.test(qqq varchar) CRUD 확인용. 인증이 없는 엔드포인트다.
# 확인이 끝나면 이 view 와 chat/urls.py 의 path("test", views.db_test) 를 반드시 제거한다.

@csrf_exempt
@require_http_methods(["GET", "POST", "PATCH", "DELETE"])
def db_test(request: HttpRequest) -> JsonResponse:
    """PK가 없어 수정·삭제는 값(qqq)으로 찾는다.

    GET                              -> 전체 조회
    POST   {"qqq": "a"}              -> 추가
    PATCH  {"old": "a", "new": "b"}  -> old와 같은 행을 모두 new로 수정
    DELETE {"qqq": "a"}              -> 같은 값의 행을 모두 삭제
    """
    if request.method == "GET":
        with connection.cursor() as cur:
            cur.execute("SELECT qqq FROM public.test")
            return JsonResponse({"rows": [r[0] for r in cur.fetchall()]})

    data, error = _body(request)
    if error is not None:
        return error

    if request.method == "POST":
        qqq = _clean_text(data.get("qqq"), 100)
        if not qqq:
            return _err("qqq 문자열(1~100자)이 필요합니다.", 400)
        with connection.cursor() as cur:
            cur.execute("INSERT INTO public.test (qqq) VALUES (%s)", [qqq])
        return JsonResponse({"created": qqq}, status=201)

    if request.method == "PATCH":
        old = _clean_text(data.get("old"), 100)
        new = _clean_text(data.get("new"), 100)
        if not old or not new:
            return _err("old, new 문자열(1~100자)이 필요합니다.", 400)
        with connection.cursor() as cur:
            cur.execute("UPDATE public.test SET qqq = %s WHERE qqq = %s", [new, old])
            return JsonResponse({"updated": cur.rowcount})

    qqq = _clean_text(data.get("qqq"), 100)  # DELETE
    if not qqq:
        return _err("qqq 문자열(1~100자)이 필요합니다.", 400)
    with connection.cursor() as cur:
        cur.execute("DELETE FROM public.test WHERE qqq = %s", [qqq])
        return JsonResponse({"deleted": cur.rowcount})
