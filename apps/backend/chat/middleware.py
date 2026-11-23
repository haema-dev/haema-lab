"""API 요청 한 줄 + 응답 한 줄. /health(K8s 프로브)는 찍지 않는다.

    api req id=... POST /api/chat/conversations/4/messages user=1
    api res id=... POST /api/chat/conversations/4/messages status=202 dur=0.05s

4xx·5xx 응답은 WARNING, 처리 중 예외는 ERROR 로 남긴다. 상태코드 자체가 분류다
(400 잘못된 요청, 401 인증 없음, 404 없음, 413 본문 큼, 429 처리 중 질문 과다, 503 큐 실패, 500 서버 에러).
Gateway 가 X-Request-Id 를 보내면 그 값을, 없으면 새로 만든 값을 쓴다.
"""

import logging
import time
import uuid

log = logging.getLogger(__name__)
SKIP_PATHS = {"/health"}


class RequestLogMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.path in SKIP_PATHS:
            return self.get_response(request)
        rid = request.META.get("HTTP_X_REQUEST_ID") or uuid.uuid4().hex[:12]
        user = request.META.get("HTTP_X_USER_ID") or "-"
        log.info("api req id=%s %s %s user=%s", rid, request.method, request.path, user)
        t0 = time.monotonic()
        try:
            response = self.get_response(request)
        except Exception:
            log.exception("api res id=%s %s %s status=500 dur=%.2fs", rid, request.method, request.path,
                          time.monotonic() - t0)
            raise
        status = response.status_code
        log.log(
            logging.INFO if status < 400 else logging.WARNING,
            "api res id=%s %s %s status=%s dur=%.2fs", rid, request.method, request.path, status,
            time.monotonic() - t0,
        )
        return response
