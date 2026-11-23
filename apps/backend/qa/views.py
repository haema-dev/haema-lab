"""Question API (architecture doc steps 1-5) and job polling.

The user id comes from the Gateway, which authenticates the request and sets
``X-User-Id``. Django is reachable only through the Gateway.
"""

from __future__ import annotations

import json
import logging
import uuid

import redis
from django.conf import settings
from django.http import HttpRequest, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from qa import repository
from qa.models import Job
from qa.redis_queue import enqueue_job

logger = logging.getLogger(__name__)

USER_ID_HEADER = "HTTP_X_USER_ID"
# A Korean question JSON-escaped as \uXXXX is 6 bytes per character:
# 300 chars -> 1,800 bytes, plus the JSON envelope.
MAX_BODY_BYTES = 2_048


def _user_id(request: HttpRequest) -> int | None:
    raw = request.META.get(USER_ID_HEADER, "")
    return int(raw) if raw.isdigit() else None


# Stateless JSON API behind the Gateway, no cookie session, so CSRF tokens protect nothing.
@csrf_exempt
@require_POST
def create_question(request: HttpRequest) -> JsonResponse:
    user_id = _user_id(request)
    if user_id is None:
        return JsonResponse({"error": "인증 정보가 없습니다."}, status=401)

    # 1. validation: reject before any DB write, queue push or FastAPI call.
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
    max_chars = settings.QA_MAX_QUESTION_CHARS
    if len(question) > max_chars:
        return JsonResponse({"error": f"질문은 {max_chars:,}자 이하여야 합니다."}, status=400)

    # 2-3. duplicate check, then message + job in one committed transaction.
    job_id, created = repository.create_question_job(user_id, question)
    if not created:
        return JsonResponse({"job_id": str(job_id), "duplicate": True}, status=202)

    # 4. queue push, once, after the commit so the worker can read the job.
    try:
        enqueue_job(job_id)
    except redis.RedisError:
        logger.exception("enqueue failed for job %s", job_id)
        # Free the duplicate slot so the user can retry; infra, not the user's failure.
        repository.mark_job_failed(
            job_id,
            failure_type=Job.FailureType.INFRA,
            detail="enqueue failed",
            from_statuses=(Job.Status.QUEUED,),
        )
        response = JsonResponse({"error": "잠시 후 다시 시도해 주세요."}, status=503)
        response["Retry-After"] = "5"
        return response

    # 5.
    return JsonResponse({"job_id": str(job_id)}, status=202)


@require_GET
def get_job(request: HttpRequest, job_id: uuid.UUID) -> JsonResponse:
    """Polling: no queue, no model, one DB read."""
    user_id = _user_id(request)
    if user_id is None:
        return JsonResponse({"error": "인증 정보가 없습니다."}, status=401)
    view = repository.get_job_view(job_id, user_id)
    if view is None:
        return JsonResponse({"error": "job을 찾을 수 없습니다."}, status=404)
    return JsonResponse(view)
